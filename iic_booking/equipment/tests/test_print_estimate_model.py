"""3D print weight / time estimate model: geometry, printer profiles, supports and support material."""

from __future__ import annotations

import struct
import time
from decimal import Decimal

import numpy as np
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.equipment.calculators import ChargeCalculationEngine
from iic_booking.equipment.fabrication import (
    OWN_MATERIAL_KEY,
    PRINT_SUPPORTS_KEY,
    build_print_parts,
    format_part_line,
    inject_print_parts,
)
from iic_booking.equipment.models import ChargeProfile, EquipmentManager, PrintAnalysis
from iic_booking.equipment.print_estimate_model import (
    ESTIMATE_KEY,
    PRESETS,
    SUPPORT_BUILDPLATE,
    SUPPORT_EVERYWHERE,
    SUPPORT_NONE,
    SupportOptions,
    compute_features,
    detect_preset,
    estimate,
    fit_calibration,
    resolve_profile,
    stl_triangles,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import funded_student, print_equipment, print_material, print_part

# --------------------------------------------------------------------------- meshes

_BOX = np.array(
    [
        ((0, 0, 0), (1, 1, 0), (1, 0, 0)), ((0, 0, 0), (0, 1, 0), (1, 1, 0)),
        ((0, 0, 1), (1, 0, 1), (1, 1, 1)), ((0, 0, 1), (1, 1, 1), (0, 1, 1)),
        ((0, 0, 0), (1, 0, 0), (1, 0, 1)), ((0, 0, 0), (1, 0, 1), (0, 0, 1)),
        ((0, 1, 0), (1, 1, 1), (1, 1, 0)), ((0, 1, 0), (0, 1, 1), (1, 1, 1)),
        ((0, 0, 0), (0, 0, 1), (0, 1, 1)), ((0, 0, 0), (0, 1, 1), (0, 1, 0)),
        ((1, 0, 0), (1, 1, 0), (1, 1, 1)), ((1, 0, 0), (1, 1, 1), (1, 0, 1)),
    ],
    dtype=np.float64,
)


def boxes(*specs):
    """Union of axis-aligned boxes (x, y, z, w, d, h) as an (n, 3, 3) triangle array."""
    parts = []
    for x, y, z, w, d, h in specs:
        parts.append(_BOX * np.array([w, d, h]) + np.array([x, y, z]))
    return np.concatenate(parts)


def cube(size):
    return boxes((0, 0, 0, size, size, size))


def mushroom():
    """Base slab, pillar and a wide cap: part of the cap's underside is above the base (only 'everywhere'
    supports it), the outer ring has a clear path to the build plate."""
    return boxes((-20, -20, 0, 40, 40, 5), (-5, -5, 5, 10, 10, 25), (-30, -30, 30, 60, 60, 5))


def pyramid(base=40.0, height=30.0):
    h = base / 2
    a, b, c, d = (-h, -h, 0), (h, -h, 0), (h, h, 0), (-h, h, 0)
    top = (0, 0, height)
    return np.array([(a, c, b), (a, d, c), (a, b, top), (b, c, top), (c, d, top), (d, a, top)], dtype=np.float64)


def uv_sphere(radius=40.0, n_lat=300, n_lon=340):
    theta = np.linspace(0, np.pi, n_lat + 1)
    phi = np.linspace(0, 2 * np.pi, n_lon + 1)[:-1]
    t, p = np.meshgrid(theta, phi, indexing="ij")
    pts = np.stack([radius * np.sin(t) * np.cos(p), radius * np.sin(t) * np.sin(p), radius * (1 - np.cos(t))], -1)
    i, j = np.meshgrid(np.arange(n_lat), np.arange(n_lon), indexing="ij")
    j1 = (j + 1) % n_lon
    a, b, c, d = pts[i, j], pts[i, j1], pts[i + 1, j], pts[i + 1, j1]
    tris = np.concatenate([np.stack([a, c, d], -2).reshape(-1, 3, 3), np.stack([a, d, b], -2).reshape(-1, 3, 3)])
    return tris


def stl_bytes(tris) -> bytes:
    tris = np.asarray(tris, dtype=np.float32)
    out = bytearray(b"test mesh".ljust(80, b" "))
    out += struct.pack("<I", len(tris))
    for tri in tris:
        out += struct.pack("<3f", 0, 0, 0) + tri.tobytes() + struct.pack("<H", 0)
    return bytes(out)


def fdm(**overrides):
    return {**PRESETS["fdm_classic"], "preset": "fdm_classic", **overrides}


def run(tris, profile=None, *, infill=100.0, density=1.24, supports=None):
    return estimate(
        compute_features(tris),
        profile or fdm(),
        infill_percent=infill,
        density_g_cm3=density,
        supports=supports,
    )


# --------------------------------------------------------------------------- geometry and model


def test_stl_reader_and_features_of_a_box():
    tris = stl_triangles(stl_bytes(boxes((5, -3, 2, 30, 20, 10))))
    f = compute_features(tris)
    assert f.volume_mm3 == pytest.approx(6000, rel=1e-4)
    assert f.area_mm2 == pytest.approx(2 * (600 + 300 + 200), rel=1e-4)
    assert f.size == pytest.approx((30, 20, 10), abs=1e-4)
    assert f.overhang_area_mm2 == pytest.approx(0, abs=1e-6)


def test_solid_cube_weight_and_time_are_in_the_slicer_range():
    b = run(cube(20))
    # 8 cm³ PLA solid = 9.92 g; slicers report 9.5-10.5 g for a 20 mm cube at 100 %.
    assert 9.3 <= b.model_g <= 10.6
    assert b.support_g == 0 and b.support_mode == SUPPORT_NONE
    assert b.total_g == pytest.approx(b.model_g + b.waste_g)
    assert 25 <= b.print_min <= 150
    assert b.total_min == pytest.approx(b.print_min + b.warmup_min)


def test_weight_and_time_grow_with_density_size_and_finer_layers():
    w = [run(cube(30), infill=i) for i in (20, 50, 100)]
    assert w[0].total_g < w[1].total_g < w[2].total_g
    assert w[0].total_min < w[2].total_min
    # Shell and skin are solid at any density: 20 % is far more than 20 % of the solid weight.
    assert w[0].model_g > 0.3 * w[2].model_g

    small, big = run(cube(20)), run(cube(40))
    assert big.total_g > 7 * small.total_g and big.total_min > 3 * small.total_min

    coarse = run(cube(30), fdm(layer_height_mm=0.2))
    fine = run(cube(30), fdm(layer_height_mm=0.1))
    assert coarse.print_min < fine.print_min
    assert coarse.model_g == pytest.approx(fine.model_g, rel=0.05)


def test_faster_printer_presets_are_faster_for_the_same_part():
    f = compute_features(cube(40))
    times = {
        key: estimate(f, {**PRESETS[key], "preset": key}, infill_percent=20, density_g_cm3=1.24).print_min
        for key in ("fdm_classic", "fdm_prusa", "fdm_bambu")
    }
    assert times["fdm_bambu"] < times["fdm_prusa"] < times["fdm_classic"]


def test_resin_time_depends_on_height_not_footprint():
    resin = {**PRESETS["resin_msla"], "preset": "resin_msla"}
    narrow = run(boxes((0, 0, 0, 10, 10, 20)), resin, supports=SupportOptions(mode=SUPPORT_NONE))
    wide = run(boxes((0, 0, 0, 60, 60, 20)), resin, supports=SupportOptions(mode=SUPPORT_NONE))
    taller = run(boxes((0, 0, 0, 10, 10, 40)), resin, supports=SupportOptions(mode=SUPPORT_NONE))
    assert wide.print_min == pytest.approx(narrow.print_min, rel=0.01)
    assert taller.print_min > 1.8 * narrow.print_min
    assert wide.model_g > 30 * narrow.model_g
    # FDM time does depend on the footprint.
    assert run(boxes((0, 0, 0, 60, 60, 20))).print_min > 10 * run(boxes((0, 0, 0, 10, 10, 20))).print_min
    # Resin parts are solid: the density setting does not change them.
    assert run(cube(20), resin, infill=20).model_g == pytest.approx(run(cube(20), resin, infill=100).model_g)
    # Resin supports raise and anchor the part.
    supported = run(cube(20), resin, supports=SupportOptions(mode=SUPPORT_EVERYWHERE))
    assert supported.support_g > 0 and supported.print_min > run(cube(20), resin, supports=SupportOptions(mode=SUPPORT_NONE)).print_min


def test_multijet_always_supports_and_sls_never_does():
    mjp = {**PRESETS["mjp_projet"], "preset": "mjp_projet"}
    sls = {**PRESETS["sls_sinterit"], "preset": "sls_sinterit"}
    assert run(mushroom(), mjp, supports=SupportOptions(mode=SUPPORT_NONE)).support_mode == SUPPORT_EVERYWHERE
    b = run(mushroom(), sls, supports=SupportOptions(mode=SUPPORT_EVERYWHERE))
    assert b.support_mode == SUPPORT_NONE and b.support_g == 0


def test_supports_none_lt_build_plate_lt_everywhere():
    tris = mushroom()
    none_, plate, everywhere = (run(tris, supports=SupportOptions(mode=m)) for m in
                                (SUPPORT_NONE, SUPPORT_BUILDPLATE, SUPPORT_EVERYWHERE))
    assert none_.support_g == 0 and none_.support_min == 0
    assert 0 < plate.support_g < everywhere.support_g
    assert none_.total_g < plate.total_g < everywhere.total_g
    assert none_.total_min < plate.total_min < everywhere.total_min
    assert none_.model_g == pytest.approx(everywhere.model_g)
    # Cap underside is 60² - 10² mm²; the 40² base hides the inner part from the build plate.
    assert everywhere.overhang_area_mm2 == pytest.approx(3500, rel=0.08)
    assert everywhere.overhang_plate_mm2 == pytest.approx(2000, rel=0.12)


def test_auto_supports_follow_the_overhangs():
    auto = run(mushroom())
    assert auto.support_mode == SUPPORT_BUILDPLATE and auto.support_mode_requested == "auto"
    for tris in (cube(30), pyramid()):
        for mode in ("auto", SUPPORT_EVERYWHERE):
            b = run(tris, supports=SupportOptions(mode=mode))
            assert b.support_g == pytest.approx(0, abs=0.05), (mode, b.support_g)
        assert run(tris).support_mode == SUPPORT_NONE


def test_support_density_and_overhang_angle_change_the_supports():
    tris = mushroom()
    light = run(tris, supports=SupportOptions(mode=SUPPORT_EVERYWHERE, density_pct=10))
    dense = run(tris, supports=SupportOptions(mode=SUPPORT_EVERYWHERE, density_pct=20))
    assert light.support_g < dense.support_g
    assert dense.support_density_pct == 20
    # A sphere's lower half: a steeper threshold supports less of it.
    sphere = uv_sphere(20, 60, 64)
    shallow = run(sphere, supports=SupportOptions(mode=SUPPORT_EVERYWHERE, angle_deg=30))
    steep = run(sphere, supports=SupportOptions(mode=SUPPORT_EVERYWHERE, angle_deg=60))
    assert shallow.overhang_area_mm2 > steep.overhang_area_mm2 > 0
    assert shallow.support_g > steep.support_g


def test_separate_support_material_uses_its_density_and_adds_tool_changes():
    tris = mushroom()
    same = run(tris, supports=SupportOptions(mode=SUPPORT_EVERYWHERE))
    pva = run(tris, supports=SupportOptions(mode=SUPPORT_EVERYWHERE, material_code="PVA", material_density_g_cm3=0.62))
    assert pva.support_g == pytest.approx(same.support_g * 0.62 / 1.24, rel=1e-6)
    assert pva.support_material_g == pytest.approx(pva.support_g) and pva.support_material_code == "PVA"
    assert same.support_material_g == 0 and same.model_material_g == pytest.approx(same.total_g)
    assert pva.model_material_g == pytest.approx(pva.total_g - pva.support_g)
    assert pva.total_min > same.total_min  # tool changes
    assert pva.waste_g > same.waste_g  # purge


def test_large_stl_is_fast():
    tris = uv_sphere(40, 300, 340)
    assert len(tris) > 200_000
    data = stl_bytes(tris)
    start = time.perf_counter()
    f = compute_features(stl_triangles(data))
    b = estimate(f, fdm(), infill_percent=20, density_g_cm3=1.24)
    elapsed = time.perf_counter() - start
    assert f.volume_mm3 == pytest.approx(4 / 3 * np.pi * 40**3, rel=0.01)
    assert b.support_g > 0
    assert elapsed < 8.0, elapsed


def test_features_round_trip_and_preset_detection():
    f = compute_features(mushroom())
    again = type(f).from_dict(f.to_dict())
    b1 = estimate(f, fdm(), infill_percent=40, density_g_cm3=1.24)
    b2 = estimate(again, fdm(), infill_percent=40, density_g_cm3=1.24)
    assert b1.to_dict() == b2.to_dict()
    assert detect_preset("Bambu Lab", "P1S", "") == "fdm_bambu"
    assert detect_preset("Prusa", "MK4S", "") == "fdm_prusa"
    assert detect_preset("Formlabs", "Form 4L", "") == "resin_formlabs"
    assert detect_preset("Phrozen", "Sonic Mighty 8K", "") == "resin_msla"
    assert detect_preset("3D Systems", "ProJet MJP 3600 Max", "") == "mjp_projet"
    assert detect_preset("Sinterit", "Lisa Pro", "") == "sls_sinterit"
    assert detect_preset("Raise3D", "Pro3 Plus", "") == "fdm_classic"


def test_calibration_fit_needs_samples_and_is_robust():
    assert fit_calibration([{"est_g": 10, "act_g": 12}])["weight_factor"] is None
    samples = [{"est_g": 10, "act_g": 12, "est_min": 60, "act_min": 90}] * 4 + [{"est_g": 10, "act_g": 100}]
    fitted = fit_calibration(samples)
    assert fitted["weight_factor"] == pytest.approx(1.2)
    assert fitted["time_factor"] == pytest.approx(1.5)
    assert fitted["weight_error_after_pct"] < fitted["weight_error_before_pct"]


# --------------------------------------------------------------------------- parts and charges


def _with_estimate(part, **est):
    part.bounding_box = {ESTIMATE_KEY: {"support_mode": SUPPORT_BUILDPLATE, "support_mode_label": "Touching build plate only",
                                        "support_g": 3.2, **est}}
    part.save(update_fields=["bounding_box"])
    return part


def _cp(equipment):
    return ChargeProfile.objects.get(equipment=equipment)


@pytest.mark.django_db
def test_parts_keep_old_analyses_unchanged(egs_factory, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    eq = print_equipment(egs_factory)
    part = print_part(eq, egs_factory.student(), print_material(eq), weight="10.2", minutes=30, quantity=2)
    (p,) = build_print_parts([part])
    assert "support_mode" not in p and "support_weight_g_total" not in p
    assert PRINT_SUPPORTS_KEY not in inject_print_parts({}, [part])


@pytest.mark.django_db
def test_separate_support_material_is_charged_at_its_rate_per_copy(egs_factory, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    eq = print_equipment(egs_factory, hourly_rate="60.00")
    pla = print_material(eq)
    pva = print_material(eq, code="PVA", price="5.0000", name="PVA support")
    part = print_part(eq, egs_factory.student(), pla, weight="10.2", minutes=30, quantity=3)
    _with_estimate(part, support_material_code="PVA", support_material_g=2.1)

    inputs = inject_print_parts({}, [part], 2)
    (p,) = inputs["_fabrication_parts"]
    assert (p["support_weight_g_each"], p["support_weight_g_total"]) == (3, 18)
    assert inputs[PRINT_SUPPORTS_KEY] == "Touching build plate only, 3 g PVA each"
    assert "supports: Touching build plate only (+3 g PVA each)" in format_part_line(p)

    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, inputs["C"])
    lines = {row["description"]: row["amount"] for row in breakdown}
    assert len(lines) == 3, list(lines)
    assert lines["gear: 11 g × 3 × 2 sets PLA (FDM) @ 1.44/g"] == pytest.approx(95.04, abs=0.05)
    assert lines["gear supports: 3 g × 3 × 2 sets PVA support @ 5.00/g"] == pytest.approx(90)
    # 66 g x 1.44 + 18 g x 5 + 180 min x 60/h = 365.04
    assert total == Decimal("365")
    assert pva.pk


@pytest.mark.django_db
def test_same_material_supports_are_in_the_model_weight(egs_factory, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    eq = print_equipment(egs_factory)
    part = print_part(eq, egs_factory.student(), print_material(eq), weight="12", minutes=30)
    _with_estimate(part, support_material_code="", support_material_g=0)
    inputs = inject_print_parts({}, [part])
    (p,) = inputs["_fabrication_parts"]
    assert p["support_weight_g_total"] == 0 and p["support_material_code"] == ""
    assert inputs[PRINT_SUPPORTS_KEY] == "Touching build plate only, ~3.2 g each"
    _total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 30)
    assert [row["description"] for row in breakdown] == ["gear: 12 g PLA (FDM) @ 1.44/g"]


@pytest.mark.django_db
def test_own_material_has_no_material_charge_for_model_or_supports(egs_factory, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    eq = print_equipment(egs_factory, hourly_rate="60.00", own_charge="0")
    pla = print_material(eq)
    print_material(eq, code="PVA", price="5.0000", name="PVA support")
    part = print_part(eq, egs_factory.student(), pla, weight="10.2", minutes=30, quantity=3)
    _with_estimate(part, support_material_code="PVA", support_material_g=2.1)
    inputs = inject_print_parts({}, [part])
    inputs[OWN_MATERIAL_KEY] = True
    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 90)
    assert not any("supports" in row["description"] or "@ 1.44/g" in row["description"] for row in breakdown)
    assert total == Decimal("90")  # machine time only


# --------------------------------------------------------------------------- API


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    settings.PRINT_3D_USE_CELERY = False
    settings.PRINT_3D_ASYNC_INLINE = False
    settings.CURAENGINE_PATH = ""
    return tmp_path


def _analyze(client, eq, material, data, **fields):
    return client.post(
        f"/api/equipments/{eq.pk}/analyze-stl/",
        {"file": SimpleUploadedFile("mushroom.stl", data, content_type="model/stl"), "material_id": material.pk,
         **fields},
        format="multipart",
    )


def _oic(egs_factory, eq):
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return egs_factory.client_for(oic)


@pytest.mark.django_db
def test_upload_with_support_choices_and_breakdown(egs_factory, media_tmp):
    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    student, _ = funded_student(egs_factory)
    client = egs_factory.client_for(student)
    data = stl_bytes(mushroom())

    results = {}
    for mode in ("none", "buildplate", "everywhere"):
        resp = _analyze(client, eq, pla, data, density_percent="20", support_mode=mode)
        assert resp.status_code == 200, resp.data
        results[mode] = resp.data
    weights = [results[m]["weight_grams"] for m in ("none", "buildplate", "everywhere")]
    times = [results[m]["estimated_time_minutes"] for m in ("none", "buildplate", "everywhere")]
    assert float(weights[0]) < float(weights[1]) < float(weights[2])
    assert times[0] < times[1] < times[2]
    est = results["buildplate"]["estimate_breakdown"]
    assert est["support_mode"] == "buildplate" and est["support_g"] > 0 and est["overhang_area_mm2"] > 3000
    assert "_features" not in results["buildplate"]["bounding_box"]
    analysis = PrintAnalysis.objects.get(pk=results["buildplate"]["id"])
    assert analysis.slicer_settings["support_mode"] == "buildplate"

    resp = _analyze(client, eq, pla, data, support_mode="sideways")
    assert resp.status_code == 400
    resp = _analyze(client, eq, pla, data, support_angle_deg="80")
    assert resp.status_code == 400
    resp = _analyze(client, eq, pla, data, support_material_id=pla.pk)
    assert resp.status_code == 400  # not offered as a support material on this printer

    # Recalculation keeps the stored support choice unless a new one is sent.
    url = f"/api/print-analyses/{analysis.pk}/recalculate/"
    resp = client.patch(url, {"material_id": pla.pk, "density_percent": 20}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["estimate_breakdown"]["support_mode"] == "buildplate"
    resp = client.patch(url, {"material_id": pla.pk, "density_percent": 20, "support_mode": "none"}, format="json")
    assert resp.data["estimate_breakdown"]["support_mode"] == "none"
    assert float(resp.data["weight_grams"]) == float(weights[0])


@pytest.mark.django_db
def test_support_material_offered_by_oic_and_preview_endpoint(egs_factory, media_tmp):
    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    pva = print_material(eq, code="PVA", price="5.0000", name="PVA support")
    pva.density_g_per_cm3 = Decimal("1.19")
    pva.save()
    student, _ = funded_student(egs_factory)
    client = egs_factory.client_for(student)

    resp = client.get(f"/api/equipments/{eq.pk}/print-materials/")
    assert resp.data["support_materials"] == []
    defaults = resp.data["support_defaults"]
    assert defaults["supports_available"] and defaults["angle_deg"] == 45 and defaults["density_pct"] > 0

    oic = _oic(egs_factory, eq)
    url = "/api/oic/fabrication-materials/equipment/"
    assert oic.patch(url, {"equipment_id": eq.pk, "print_estimate_support_material_ids": [999999]},
                     format="json").status_code == 400
    resp = oic.patch(url, {"equipment_id": eq.pk, "print_estimate_support_material_ids": [pva.pk]}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["equipment"]["print_estimate"]["support_material_ids"] == [pva.pk]
    resp = client.get(f"/api/equipments/{eq.pk}/print-materials/")
    assert [m["code"] for m in resp.data["support_materials"]] == ["PVA"]

    resp = _analyze(client, eq, pla, stl_bytes(mushroom()), support_mode="everywhere", support_material_id=pva.pk)
    assert resp.status_code == 200, resp.data
    est = resp.data["estimate_breakdown"]
    assert est["support_material_code"] == "PVA" and est["support_material_g"] > 0
    analysis = PrintAnalysis.objects.get(pk=resp.data["id"])
    assert analysis.weight_grams == pytest.approx(Decimal(int(np.ceil(est["model_material_g"]))))
    stored = (analysis.weight_grams, analysis.estimated_time_minutes, dict(analysis.slicer_settings))

    preview = client.get(f"/api/print-analyses/{analysis.pk}/estimate/", {"support_mode": "none"})
    assert preview.status_code == 200, preview.data
    assert preview.data["support_weight_grams"] == 0
    assert preview.data["weight_grams"] < float(stored[0]) + 1
    assert preview.data["estimated_time_minutes"] < stored[1]
    same = client.get(f"/api/print-analyses/{analysis.pk}/estimate/")
    assert same.data["support_weight_grams"] == int(np.ceil(est["support_material_g"]))
    assert same.data["estimated_time_minutes"] == stored[1]
    analysis.refresh_from_db()
    assert (analysis.weight_grams, analysis.estimated_time_minutes, analysis.slicer_settings) == stored
    other = egs_factory.client_for(egs_factory.student())
    assert other.get(f"/api/print-analyses/{analysis.pk}/estimate/").status_code == 404


@pytest.mark.django_db
def test_oic_edits_the_estimate_profile(egs_factory):
    eq = print_equipment(egs_factory)
    eq.make, eq.model_information = "Bambu Lab", "P1S"
    eq.save(update_fields=["make", "model_information"])
    oic = _oic(egs_factory, eq)
    url = "/api/oic/fabrication-materials/equipment/"
    rows = {r["equipment_id"]: r for r in oic.get(url).data["equipments"]}
    payload = rows[eq.pk]["print_estimate"]
    assert payload["detected_preset"] == "fdm_bambu" and payload["technology"] == "FDM"
    keys = {p["key"] for p in payload["parameters"]}
    assert "perimeter_speed_mm_s" in keys and "per_layer_s" not in keys and "support_interface_layers" in keys

    bad = oic.patch(url, {"equipment_id": eq.pk, "print_estimate_overrides": {"perimeter_speed_mm_s": -4}},
                    format="json")
    assert bad.status_code == 400
    resp = oic.patch(
        url, {"equipment_id": eq.pk, "print_estimate_overrides": {"perimeter_speed_mm_s": 80, "warmup_min": 3}},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    eq.refresh_from_db()
    assert resolve_profile(eq)["perimeter_speed_mm_s"] == 80 and resolve_profile(eq)["warmup_min"] == 3

    resp = oic.patch(url, {"equipment_id": eq.pk, "print_estimate_preset": "resin_formlabs"}, format="json")
    assert resp.status_code == 200
    eq.refresh_from_db()
    assert resolve_profile(eq)["technology"] == "RESIN" and "overrides" not in eq.print_estimate_profile

    resp = oic.patch(url, {"equipment_id": eq.pk, "print_estimate_calibration": "fit"}, format="json")
    assert resp.status_code == 200
    assert resp.data["equipment"]["print_estimate"]["calibration"]["weight_factor"] is None
    resp = oic.patch(url, {"equipment_id": eq.pk, "print_estimate_calibration": "apply"}, format="json")
    assert resp.status_code == 400
