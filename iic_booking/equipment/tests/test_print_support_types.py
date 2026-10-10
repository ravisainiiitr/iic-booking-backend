"""3D print support types (normal / lines / zigzag / snug / concentric / gyroid / tree / organic, resin densities),
support interface and brim / raft: overhang estimation on simple meshes, per-type factors, charges, the OIC's
per-printer options and backward compatibility with estimates made before them."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from iic_booking.equipment.calculators import ChargeCalculationEngine
from iic_booking.equipment.fabrication import (
    PRINT_SUPPORTS_KEY,
    build_print_parts,
    format_part_line,
    inject_print_parts,
    print_part_options_text,
)
from iic_booking.equipment.models import ChargeProfile, PrintAnalysis
from iic_booking.equipment.print_estimate_model import (
    ADHESION_BRIM,
    ADHESION_RAFT,
    ESTIMATE_KEY,
    PRESETS,
    SUPPORT_BUILDPLATE,
    SUPPORT_EVERYWHERE,
    SUPPORT_NONE,
    SUPPORT_TYPES,
    SupportOptions,
    clean_support_options,
    compute_features,
    resolve_profile,
    resolved_support_options,
)

from .fabrication_helpers import funded_student, print_equipment, print_material, print_part
from .test_print_estimate_model import _analyze, _oic, boxes, cube, run, stl_bytes


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    settings.PRINT_3D_USE_CELERY = False
    settings.PRINT_3D_ASYNC_INLINE = False
    settings.CURAENGINE_PATH = ""
    return tmp_path


def t_shape():
    """A 10 mm stem with a 40 x 40 cap on top: the cap's underside (40² - 10²) overhangs 20 mm above the plate."""
    return boxes((-5, -5, 0, 10, 10, 20), (-20, -20, 20, 40, 40, 5))


def bridge():
    """Two 10 x 10 pillars 20 mm apart under a 40 x 10 deck: only the 20 x 10 span needs support."""
    return boxes((0, 0, 0, 10, 10, 20), (30, 0, 0, 10, 10, 20), (0, 0, 20, 40, 10, 5))


def everywhere(**kw):
    return SupportOptions(mode=SUPPORT_EVERYWHERE, **kw)


# --------------------------------------------------------------------------- overhang estimation


def test_overhang_area_and_support_volume_of_simple_meshes():
    for tris, area, volume in ((t_shape(), 1500.0, 30000.0), (bridge(), 200.0, 4000.0)):
        f = compute_features(tris)
        a_plate, v_plate, top = f.support_at(SUPPORT_BUILDPLATE, 45)
        assert a_plate == pytest.approx(area, rel=0.1)
        assert v_plate == pytest.approx(volume, rel=0.1)
        assert top == pytest.approx(20, abs=0.5)
    f = compute_features(cube(30))
    assert f.support_at(SUPPORT_EVERYWHERE, 45) == (0.0, 0.0, 0.0)
    for key in SUPPORT_TYPES:
        if SUPPORT_TYPES[key]["technology"] == "FDM":
            b = run(cube(30), supports=everywhere(type=key))
            assert b.support_g == 0 and b.support_type == key


def test_support_types_scale_the_columns_and_change_the_time():
    tris = t_shape()
    by_type = {k: run(tris, supports=everywhere(type=k, interface=False)) for k in SUPPORT_TYPES
               if SUPPORT_TYPES[k]["technology"] == "FDM"}
    normal = by_type["normal"]
    # 12 % grid under ~1500 mm² x 20 mm of PLA.
    assert normal.support_g == pytest.approx(30000 * 0.12 / 1000 * 1.24, rel=0.1)
    for key, b in by_type.items():
        assert b.support_g == pytest.approx(normal.support_g * SUPPORT_TYPES[key]["volume_factor"], rel=1e-6), key
        assert b.model_g == pytest.approx(normal.model_g)
        assert b.to_dict()["support_type_label"] == SUPPORT_TYPES[key]["label"]
    assert by_type["organic"].support_g < by_type["tree"].support_g < by_type["lines"].support_g
    assert by_type["lines"].support_g < normal.support_g < by_type["gyroid"].support_g
    # Tree: ~45 % less material at a slower speed is still quicker; gyroid is heavier and slower.
    assert by_type["tree"].support_min < normal.support_min < by_type["gyroid"].support_min
    assert by_type["zigzag"].support_min < normal.support_min


def test_support_interface_adds_dense_layers_and_placement_still_applies():
    tris = t_shape()
    on, off = run(tris, supports=everywhere()), run(tris, supports=everywhere(interface=False))
    assert on.support_g > off.support_g and on.support_interface and not off.support_interface
    tree_plate = run(bridge(), supports=SupportOptions(mode=SUPPORT_BUILDPLATE, type="tree"))
    tree_none = run(bridge(), supports=SupportOptions(mode=SUPPORT_NONE, type="tree"))
    assert tree_plate.support_g > 0 and tree_plate.support_type == "tree"
    assert tree_none.support_g == 0 and tree_none.support_type == ""


def test_brim_and_raft_are_estimated_from_the_first_layer():
    none_, brim, raft = (run(cube(20), supports=SupportOptions(adhesion=a)) for a in ("none", ADHESION_BRIM, ADHESION_RAFT))
    assert none_.adhesion_g == 0 and none_.adhesion_min == 0
    # Brim: 80 mm outline x 5 mm + corners, one 0.2 mm first layer.
    assert brim.adhesion_g == pytest.approx((80 * 5 + math.pi * 25) * 0.2 / 1000 * 1.24, rel=0.05)
    # Raft: 20 x 20 footprint grown by 3 mm, 0.9 mm thick at 70 %.
    assert raft.adhesion_g == pytest.approx((400 + 80 * 3 + math.pi * 9) * 0.9 * 0.7 / 1000 * 1.24, rel=0.05)
    for b in (brim, raft):
        assert b.total_g == pytest.approx(b.model_g + b.support_g + b.adhesion_g + b.waste_g)
        assert b.model_material_g == pytest.approx(b.total_g)
        assert b.model_g == pytest.approx(none_.model_g)
    assert none_.total_min < brim.total_min < raft.total_min
    d = raft.to_dict()
    assert d["adhesion"] == "raft" and d["adhesion_label"] == "Raft" and d["adhesion_g"] > 0
    # The raft also covers the supports that stand on the plate.
    supported = run(t_shape(), supports=SupportOptions(mode=SUPPORT_BUILDPLATE, adhesion=ADHESION_RAFT))
    bare = run(t_shape(), supports=SupportOptions(mode=SUPPORT_NONE, adhesion=ADHESION_RAFT))
    assert supported.adhesion_g > bare.adhesion_g


def test_resin_support_densities_and_technology_specific_types():
    resin = {**PRESETS["resin_msla"], "preset": "resin_msla"}
    light, medium, heavy = (run(t_shape(), resin, supports=everywhere(type=k))
                            for k in ("resin_light", "resin_medium", "resin_heavy"))
    assert light.support_g < medium.support_g < heavy.support_g
    assert medium.support_type == "resin_medium"
    # An FDM type on a resin printer falls back to the printer's default; resin has no brim / raft choice.
    fallback = run(t_shape(), resin, supports=everywhere(type="tree", adhesion=ADHESION_RAFT))
    assert fallback.support_g == pytest.approx(medium.support_g) and fallback.adhesion_g == 0
    sls = {**PRESETS["sls_sinterit"], "preset": "sls_sinterit"}
    assert run(t_shape(), sls, supports=everywhere(type="tree")).support_type == ""
    assert [t["key"] for t in resolved_support_options({}, "SLS")["types"]] == []
    assert resolved_support_options({}, "RESIN")["adhesion"] == []


def test_estimates_without_the_new_settings_are_unchanged():
    tris = t_shape()
    legacy = SupportOptions.from_settings({"support_mode": "buildplate", "support_density_pct": 15})
    assert (legacy.type, legacy.interface, legacy.adhesion) == ("", None, "none")
    explicit = SupportOptions(mode=SUPPORT_BUILDPLATE, density_pct=15, type="normal", interface=True, adhesion="none")
    a, b = run(tris, supports=legacy).to_dict(), run(tris, supports=explicit).to_dict()
    assert a == b
    # Same numbers as the model before support types: column x density + interface, no adhesion.
    f = compute_features(tris)
    area, raw, _top = f.support_at(SUPPORT_BUILDPLATE, 45)
    mm3 = raw * 0.15 + area * 2 * 0.1 * 0.7
    assert a["support_g"] == pytest.approx(mm3 / 1000 * 1.24, rel=1e-3)
    assert a["adhesion_g"] == 0 and a["support_volume_factor"] == 1.0


def test_oic_factor_overrides_reach_the_estimate():
    stored = {"support_options": {"factors": {"tree": {"volume_factor": 0.3, "speed_factor": 0.5}}}}
    profile = resolve_profile(None, stored)
    assert profile["support_type_factors"]["tree"] == {"volume_factor": 0.3, "speed_factor": 0.5}
    assert profile["support_type_default"] == "normal"
    base = run(t_shape(), profile, supports=everywhere(type="normal", interface=False))
    tree = run(t_shape(), profile, supports=everywhere(type="tree", interface=False))
    assert tree.support_g == pytest.approx(base.support_g * 0.3, rel=1e-6)

    cleaned, err = clean_support_options({"types": ["tree", "organic"], "default_type": "organic",
                                          "adhesion": ["brim"]}, "FDM")
    assert err is None and cleaned["adhesion"] == ["none", "brim"]
    options = resolved_support_options({"support_options": cleaned}, "FDM")
    assert [t["key"] for t in options["types"] if t["enabled"]] == ["tree", "organic"]
    assert options["default_type"] == "organic"
    assert [a["key"] for a in options["adhesion"] if a["enabled"]] == ["none", "brim"]
    assert resolve_profile(None, {"support_options": cleaned})["support_type_default"] == "organic"
    for bad in ({"types": []}, {"types": ["warp"]}, {"types": ["tree"], "default_type": "normal"},
                {"factors": {"tree": {"volume_factor": 9}}}, {"factors": {"tree": {"speed_factor": "x"}}},
                {"adhesion": ["glue"]}, "tree"):
        assert clean_support_options(bad, "FDM")[0] is None, bad


# --------------------------------------------------------------------------- parts and charges


def _with_estimate(part, **est):
    part.bounding_box = {ESTIMATE_KEY: est}
    part.save(update_fields=["bounding_box"])
    return part


NEW_ESTIMATE = {
    "support_mode": SUPPORT_BUILDPLATE, "support_mode_label": "Touching build plate only", "support_g": 3.2,
    "support_type": "tree", "support_type_label": "Tree", "support_interface": True, "adhesion": "raft",
    "adhesion_label": "Raft", "adhesion_g": 0.5, "model_g": 8.0, "waste_g": 0.5, "support_material_code": "",
    "support_material_g": 0,
}


@pytest.mark.django_db
def test_parts_charges_and_texts_show_the_support_type_and_raft(egs_factory, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    eq = print_equipment(egs_factory)
    part = print_part(eq, egs_factory.student(), print_material(eq), weight="13", minutes=30, quantity=2)
    _with_estimate(part, **NEW_ESTIMATE)
    inputs = inject_print_parts({}, [part])
    (p,) = inputs["_fabrication_parts"]
    assert (p["support_type"], p["adhesion"], p["adhesion_g_each"], p["model_g_each"]) == ("tree", "raft", 0.5, 8.0)
    assert p["weight_composition"] == "model 8.0 g + supports 3.2 g + raft 0.5 g + purge 0.5 g"
    assert inputs[PRINT_SUPPORTS_KEY] == "Tree (touching build plate only), ~3.2 g each, raft ~0.5 g each"
    assert print_part_options_text(p) == "Tree supports (touching build plate only), raft"
    assert "supports: Tree (touching build plate only), raft ~0.5 g each" in format_part_line(p)

    _total, breakdown = ChargeCalculationEngine.calculate_charge(ChargeProfile.objects.get(equipment=eq), inputs, 60)
    (row,) = breakdown
    assert row["description"] == (
        "gear: 13 g × 2 PLA (FDM) @ 1.44/g (model 8.0 g + supports 3.2 g + raft 0.5 g + purge 0.5 g each)"
    )
    assert row["amount"] == pytest.approx(26 * 1.44, abs=0.5)

    # Staff-entered actual weight: no estimate make-up on the line.
    part.actual_weight_grams = Decimal("30")
    part.save(update_fields=["actual_weight_grams"])
    inputs = inject_print_parts({}, [part])
    _total, breakdown = ChargeCalculationEngine.calculate_charge(ChargeProfile.objects.get(equipment=eq), inputs, 60)
    assert breakdown[0]["description"] == "gear: 30 g (actual) PLA (FDM) @ 1.44/g"


@pytest.mark.django_db
def test_old_estimates_keep_their_part_rows_and_charge_lines(egs_factory, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    eq = print_equipment(egs_factory)
    part = print_part(eq, egs_factory.student(), print_material(eq), weight="12", minutes=30)
    _with_estimate(part, support_mode=SUPPORT_BUILDPLATE, support_mode_label="Touching build plate only",
                   support_g=3.2, support_material_code="", support_material_g=0)
    (p,) = build_print_parts([part])
    assert not {"support_type", "adhesion", "model_g_each", "weight_composition"} & set(p)
    assert print_part_options_text(p) == ""
    inputs = inject_print_parts({}, [part])
    assert inputs[PRINT_SUPPORTS_KEY] == "Touching build plate only, ~3.2 g each"
    _total, breakdown = ChargeCalculationEngine.calculate_charge(ChargeProfile.objects.get(equipment=eq), inputs, 30)
    assert [row["description"] for row in breakdown] == ["gear: 12 g PLA (FDM) @ 1.44/g"]


# --------------------------------------------------------------------------- API


@pytest.mark.django_db
def test_oic_enables_types_and_users_book_with_them(egs_factory, media_tmp):
    eq = print_equipment(egs_factory)
    pla = print_material(eq)
    student, _ = funded_student(egs_factory)
    client = egs_factory.client_for(student)

    defaults = client.get(f"/api/equipments/{eq.pk}/print-materials/").data["support_defaults"]
    assert [t["key"] for t in defaults["support_types"]] == [
        "normal", "lines", "zigzag", "snug", "concentric", "gyroid", "tree", "organic"
    ]
    assert defaults["default_support_type"] == "normal" and defaults["interface_layers"] == 2
    assert [a["key"] for a in defaults["adhesion_types"]] == ["none", "brim", "raft"]

    oic = _oic(egs_factory, eq)
    url = "/api/oic/fabrication-materials/equipment/"
    body = {"types": ["normal", "tree", "organic"], "default_type": "tree", "adhesion": ["brim"],
            "factors": {"tree": {"volume_factor": 0.5}}}
    resp = oic.patch(url, {"equipment_id": eq.pk, "print_estimate_support_options": body}, format="json")
    assert resp.status_code == 200, resp.data
    options = resp.data["equipment"]["print_estimate"]["support_options"]
    tree = next(t for t in options["types"] if t["key"] == "tree")
    assert tree["enabled"] and tree["volume_factor"] == 0.5 and tree["default_volume_factor"] == 0.55
    assert options["default_type"] == "tree"
    bad = oic.patch(url, {"equipment_id": eq.pk, "print_estimate_support_options": {"types": []}}, format="json")
    assert bad.status_code == 400

    defaults = client.get(f"/api/equipments/{eq.pk}/print-materials/").data["support_defaults"]
    assert [t["key"] for t in defaults["support_types"]] == ["normal", "tree", "organic"]
    assert defaults["default_support_type"] == "tree"
    assert [a["key"] for a in defaults["adhesion_types"]] == ["none", "brim"]

    data = stl_bytes(t_shape())
    assert _analyze(client, eq, pla, data, support_type="lines").status_code == 400
    assert _analyze(client, eq, pla, data, adhesion="raft").status_code == 400
    normal = _analyze(client, eq, pla, data, support_mode="everywhere", support_type="normal")
    tree = _analyze(client, eq, pla, data, support_mode="everywhere", support_type="tree", adhesion="brim",
                    support_interface="false")
    assert normal.status_code == 200 and tree.status_code == 200, (normal.data, tree.data)
    est = tree.data["estimate_breakdown"]
    assert (est["support_type"], est["adhesion"], est["support_interface"]) == ("tree", "brim", False)
    assert est["adhesion_g"] > 0 and est["support_g"] < normal.data["estimate_breakdown"]["support_g"]
    analysis = PrintAnalysis.objects.get(pk=tree.data["id"])
    assert analysis.slicer_settings["support_type"] == "tree" and analysis.slicer_settings["adhesion"] == "brim"
    assert analysis.slicer_settings["support_interface"] is False

    # What-if preview and recalculation keep the stored choice unless a new one is sent.
    preview = client.get(f"/api/print-analyses/{analysis.pk}/estimate/", {"support_type": "organic"})
    assert preview.status_code == 200 and preview.data["estimate_breakdown"]["support_type"] == "organic"
    recalc = client.patch(f"/api/print-analyses/{analysis.pk}/recalculate/", {"material_id": pla.pk}, format="json")
    assert recalc.status_code == 200, recalc.data
    assert recalc.data["estimate_breakdown"]["support_type"] == "tree"
    recalc = client.patch(f"/api/print-analyses/{analysis.pk}/recalculate/",
                          {"material_id": pla.pk, "adhesion": "", "support_type": ""}, format="json")
    assert recalc.data["estimate_breakdown"]["adhesion"] == "none"
    assert recalc.data["estimate_breakdown"]["support_type"] == "tree"  # the printer's default
