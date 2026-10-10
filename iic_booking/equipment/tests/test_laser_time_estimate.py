"""Machine-time estimate for 2D laser / profile cutting from the DXF cut path."""

from __future__ import annotations

import io
import math
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.equipment.calculators import TimeCalculationEngine
from iic_booking.equipment.fabrication import (
    BOOKED_MINUTES_KEY,
    LASER_ESTIMATE_KEY,
    apply_fabrication_to_input_values,
    format_part_line,
    format_seconds,
    laser_booking_time_estimate,
)
from iic_booking.equipment.laser_cut_service import analyze_dxf_bytes
from iic_booking.equipment.laser_time_model import (
    PRESETS,
    JobPart,
    clean_material_overrides,
    clean_profile_overrides,
    detect_preset,
    estimate_job,
    estimate_part,
    junction_speed,
    material_cut_params,
    resolve_profile,
)
from iic_booking.equipment.models import Booking, ChargeProfile, LaserCutAnalysis
from iic_booking.equipment.serializers import BookingSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import acrylic_3mm, dxf_bytes, funded_student, laser_equipment, laser_part


def _dxf(build, units=4) -> bytes:
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = units
    build(doc.modelspace())
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def _plate(msp):
    """200 x 100 plate, four r=5 holes and a slot with semicircular ends (bulged polyline)."""
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    for x in (20, 180):
        for y in (20, 80):
            msp.add_circle((x, y), 5)
    msp.add_lwpolyline(
        [(60, 45, 0, 0, 0), (140, 45, 0, 0, 1), (140, 55, 0, 0, 0), (60, 55, 0, 0, 1)], format="xyseb", close=True
    )


PLATE_CUT_MM = 2 * (200 + 100) + 4 * 2 * math.pi * 5 + 2 * 80 + 2 * math.pi * 5


def _material(family="ACRYLIC", thickness=3, pk=1, width=2438.4, height=1219.2):
    return SimpleNamespace(
        pk=pk, material_family=family, thickness_mm=Decimal(str(thickness)), sheet_width_mm=width, sheet_height_mm=height
    )


# ------------------------------------------------------------------------------------------- geometry


def test_plate_cut_path_length_contours_and_corners():
    f = analyze_dxf_bytes(_dxf(_plate)).cut_features
    assert f["cut_length"] == pytest.approx(PLATE_CUT_MM, rel=0.002)
    assert (f["contours"], f["closed"], f["open"]) == (6, 6, 0)
    assert f["corners"] == 4  # the plate's corners; holes and the slot are smooth
    assert f["duplicates"] == 0
    assert f["travel_length"] > 0


def test_separate_lines_are_chained_and_duplicates_dropped_with_a_warning():
    def build(msp):
        for a, b in (((0, 0), (50, 0)), ((50, 0), (50, 50)), ((50, 50), (0, 50)), ((0, 50), (0, 0))):
            msp.add_line(a, b)
            msp.add_line(b, a)

    result = analyze_dxf_bytes(_dxf(build))
    f = result.cut_features
    assert f["cut_length"] == pytest.approx(200)
    assert (f["contours"], f["closed"], f["duplicates"], f["corners"]) == (1, 1, 4, 4)
    assert any("duplicate" in w for w in result.warnings)


def test_open_paths_are_one_pierce_each():
    def build(msp):
        msp.add_line((0, 0), (100, 0))
        msp.add_line((0, 10), (100, 10))
        msp.add_lwpolyline([(0, 20), (50, 20), (50, 40)])

    f = analyze_dxf_bytes(_dxf(build)).cut_features
    assert (f["contours"], f["open"]) == (3, 3)
    assert f["cut_length"] == pytest.approx(270)


# ------------------------------------------------------------------------------------------- model


def test_junction_speed_falls_with_the_direction_change():
    v, a, dev = 50.0, 2000.0, 0.05
    assert junction_speed(0, v, a, dev) == v
    assert 0 < junction_speed(120, v, a, dev) < junction_speed(45, v, a, dev) < v
    assert junction_speed(180, v, a, dev) == 0


def test_chart_values_interpolate_override_and_warn_for_unsuitable_material():
    co2 = resolve_profile(None, {"preset": "co2_laser"})
    exact = material_cut_params(co2, _material("ACRYLIC", 3))
    assert (exact.speed_mm_s, exact.pierce_s) == pytest.approx((16, 0.2))
    between = material_cut_params(co2, _material("ACRYLIC", 4))
    assert 9 < between.speed_mm_s < 16
    assert material_cut_params(co2, _material("MS", 2)).warning
    assert not material_cut_params(resolve_profile(None, {"preset": "fiber_laser"}), _material("MS", 2)).warning

    tuned = resolve_profile(None, {"preset": "co2_laser", "material_overrides": {"1": {"cut_speed_mm_s": 25}}})
    cut = material_cut_params(tuned, _material("ACRYLIC", 3))
    assert cut.speed_mm_s == 25 and cut.overridden and cut.chart_speed_mm_s == pytest.approx(16)

    faster = resolve_profile(None, {"preset": "co2_laser", "overrides": {"cut_speed_factor_pct": 150}})
    assert material_cut_params(faster, _material("ACRYLIC", 3)).speed_mm_s == pytest.approx(24)


def test_part_time_scales_with_speed_pierces_and_units():
    f = analyze_dxf_bytes(_dxf(_plate)).cut_features
    profile = resolve_profile(None, {"preset": "co2_laser"})
    slow = estimate_part(f, 1.0, profile, material_cut_params(profile, _material("ACRYLIC", 6)))
    fast = estimate_part(f, 1.0, profile, material_cut_params(profile, _material("ACRYLIC", 2)))
    assert slow.cut_s > 2.5 * fast.cut_s
    # Straight runs dominate, so cutting time is close to length / speed and never below it.
    assert slow.cut_s >= PLATE_CUT_MM / 7.0 * 0.99
    assert slow.cut_s < PLATE_CUT_MM / 7.0 * 1.3
    assert slow.pierces == 6
    assert slow.pierce_s == pytest.approx(6 * (0.4 + profile["pierce_settle_s"]))

    inches = estimate_part(f, 25.4, profile, material_cut_params(profile, _material("ACRYLIC", 6)))
    assert inches.cut_length_mm == pytest.approx(f["cut_length"] * 25.4)
    assert inches.cut_s > 20 * slow.cut_s


def test_job_adds_setup_sheets_and_allowance_and_multiplies_copies():
    f = analyze_dxf_bytes(_dxf(_plate)).cut_features
    profile = resolve_profile(None, {"preset": "fiber_laser"})
    steel = _material("MS", 2)
    one = estimate_job(profile, [JobPart("a", f, 1.0, steel, 1, 20000)])
    ten = estimate_job(profile, [JobPart("a", f, 1.0, steel, 10, 20000)])
    assert one.setup_min == 15 and one.sheets == 1 and one.sheet_min == 5
    assert ten.cutting_min == pytest.approx(10 * one.cutting_min)
    expected = (15 + 5 + ten.cutting_min) * 1.10
    assert ten.total_min == math.ceil(expected - 1e-9)
    assert ten.parts["a"]["minutes_total"] == pytest.approx(round(ten.cutting_min, 1), abs=0.1)

    # 400 copies of 200 x 100 on a 2438 x 1219 sheet at 80 % nesting need 4 sheets.
    many = estimate_job(profile, [JobPart("a", f, 1.0, steel, 400, 20000)])
    assert many.sheets == math.ceil(400 * 20000 / (2438.4 * 1219.2 * 0.8))


@pytest.mark.parametrize(
    "fields, preset",
    [
        ({"name": "CNC Laser Cutting Machine", "code": "CNCL1"}, "co2_laser"),
        ({"name": "Metal Laser Cutter", "code": "MLC1"}, "fiber_laser"),
        ({"name": "Fibre laser", "make": "Bodor"}, "fiber_laser"),
        ({"name": "High Pressure Waterjet", "code": "HPW1"}, "waterjet"),
        ({"name": "CNC Router", "code": "CNCR1"}, "cnc_router"),
        ({"name": "Vertical Machining Centre", "code": "VMC1"}, "vmc_milling"),
        ({"name": "Plasma cutter"}, "plasma"),
        ({"name": "Machine", "code": "X1"}, "co2_laser"),
    ],
)
def test_machine_type_is_detected_from_the_equipment_text(fields, preset):
    assert detect_preset(**fields) == preset


def test_override_validation():
    assert clean_profile_overrides({"setup_min": "12", "allowance_pct": ""}) == ({"setup_min": 12.0}, None)
    assert clean_profile_overrides({"bogus": 1})[1]
    assert "between" in clean_profile_overrides({"acceleration_mm_s2": 1})[1]
    assert clean_material_overrides({"7": {"cut_speed_mm_s": "20", "pierce_s": ""}}) == (
        {"7": {"cut_speed_mm_s": 20.0}},
        None,
    )
    assert clean_material_overrides({"x": {"cut_speed_mm_s": 1}})[1]
    assert clean_material_overrides({"7": {"pierce_s": -1}})[1]


def test_format_seconds():
    assert [format_seconds(s) for s in (12.4, 60, 125, 3600, 3725)] == ["12 s", "1 min", "2 min 5 s", "1 h", "1 h 2 min"]


# ------------------------------------------------------------------------------------------- booking flow


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    return tmp_path


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _upload(client, eq, name, data, **extra):
    return client.post(
        f"/api/equipments/{eq.pk}/analyze-dxf/",
        {"file": SimpleUploadedFile(name, data, content_type="application/dxf"), **extra},
        format="multipart",
    )


@pytest.mark.django_db
def test_upload_quote_and_booking_use_the_dxf_estimate(egs_factory, media_tmp, no_portal_lock):
    eq = laser_equipment(egs_factory, hourly_rate="120.00", name="CO2 Laser Cutter")
    acr = acrylic_3mm(eq)
    student, _sub = funded_student(egs_factory)
    client = egs_factory.client_for(student)

    batch = _upload(client, eq, "plate.dxf", _dxf(_plate), material_id=acr.pk).data
    item = batch["items"][0]
    est = item["time_estimate"]
    assert est["pierces"] == 6
    assert est["cut_length_mm"] == pytest.approx(PLATE_CUT_MM, rel=0.002)
    assert est["seconds_each"] > 0
    assert client.patch(f"/api/laser-cut-analyses/{item['id']}/", {"quantity": 4}, format="json").status_code == 200

    resp = client.get(f"/api/equipments/{eq.pk}/calculate/?laser_cut_batch_id={batch['id']}")
    assert resp.status_code == 200, resp.data
    job = resp.data["input_values"][LASER_ESTIMATE_KEY]
    assert job["preset"] == "co2_laser"
    assert resp.data["total_time_minutes"] == job["total_min"]
    # setup 10 + 1 sheet x 3 + 4 copies, +10 %
    assert job["total_min"] == math.ceil((10 + 3 + 4 * est["seconds_each"] / 60) * 1.1 - 1e-9)
    machine = [line for line in resp.data["charge_breakdown"] if "machine time" in line["description"]]
    assert machine and machine[0]["description"].startswith(f"{job['total_min']} min machine time")

    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    resp = client.post(
        f"/api/equipments/{eq.pk}/book/",
        {
            "slot_ids": [slot.pk],
            "start_time": slot.start_datetime.isoformat(),
            "end_time": slot.end_datetime.isoformat(),
            "input_values": {},
            "laser_cut_batch_id": batch["id"],
        },
        format="json",
    )
    assert resp.status_code in (200, 201), resp.data
    booking = Booking.objects.get(user=student, equipment=eq)
    assert LASER_ESTIMATE_KEY not in (booking.input_values or {})

    # Recalculation after booking keeps the estimate, not the booked slot length.
    inputs = apply_fabrication_to_input_values(booking, booking.input_values)
    assert inputs[BOOKED_MINUTES_KEY] == 60 != job["total_min"]
    cp = ChargeProfile.objects.get(equipment=eq)
    assert TimeCalculationEngine.calculate_time(cp, inputs, slot_duration_minutes=60) == job["total_min"]

    data = BookingSerializer(booking, context={"request": None}).data
    assert data["laser_time_estimate"]["total_min"] == job["total_min"]
    part = data["fabrication_parts"][0]
    assert part["time_estimate"]["pierces"] == 6
    assert "est." in format_part_line(part) and "6 pierces" in format_part_line(part)


@pytest.mark.django_db
def test_parts_without_a_measured_cut_path_keep_the_booked_duration(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), slot_count=2)
    laser_part(eq, owner, acrylic_3mm(eq), booking=booking)

    inputs = apply_fabrication_to_input_values(booking, {})
    assert LASER_ESTIMATE_KEY not in inputs
    assert TimeCalculationEngine.calculate_time(ChargeProfile.objects.get(equipment=eq), inputs, 60) == 120
    assert laser_booking_time_estimate(booking) is None


@pytest.mark.django_db
def test_backfill_command_measures_old_parts(egs_factory, media_tmp):
    from django.core.management import call_command

    eq = laser_equipment(egs_factory, name="Metal Laser Cutter")
    owner = egs_factory.student()
    part = laser_part(eq, owner, acrylic_3mm(eq), data=dxf_bytes(rects=((0, 0, 200, 100),)))
    assert not part.cut_features

    out = io.StringIO()
    call_command("backfill_laser_cut_features", stdout=out)
    assert "DRY RUN" in out.getvalue() and "detected_type=fiber_laser" in out.getvalue()
    part.refresh_from_db()
    assert not part.cut_features

    call_command("backfill_laser_cut_features", "--apply", "--pin-detected-preset", stdout=io.StringIO())
    part.refresh_from_db()
    eq.refresh_from_db()
    assert part.cut_features["cut_length"] == pytest.approx(600)
    assert part.cut_features["contours"] == 1
    assert eq.laser_estimate_profile == {"preset": "fiber_laser"}


@pytest.mark.django_db
def test_oic_page_shows_and_saves_the_estimate_settings(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory, name="Laser cutter")
    acr = acrylic_3mm(eq)
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True, admin_approved=True)
    client = egs_factory.client_for(admin)
    url = "/api/oic/fabrication-materials/equipment/"

    rows = {r["equipment_id"]: r for r in client.get(url).data["equipments"]}
    est = rows[eq.pk]["laser_estimate"]
    assert est["effective_preset"] == "co2_laser" and est["preset"] == ""
    assert {p["key"] for p in est["presets"]} == set(PRESETS)
    mat = next(m for m in est["materials"] if m["material_id"] == acr.pk)
    assert mat["chart_cut_speed_mm_s"] == pytest.approx(16) and mat["cut_speed_mm_s"] is None

    resp = client.patch(
        url,
        {
            "equipment_id": eq.pk,
            "laser_estimate_preset": "waterjet",
            "laser_estimate_overrides": {"setup_min": 25},
            "laser_estimate_material_overrides": {str(acr.pk): {"cut_speed_mm_s": 30, "pierce_s": 2}},
        },
        format="json",
    )
    assert resp.status_code == 200, resp.data
    est = resp.data["equipment"]["laser_estimate"]
    assert est["effective_preset"] == "waterjet"
    assert next(p for p in est["parameters"] if p["key"] == "setup_min")["value"] == 25
    assert next(m for m in est["materials"] if m["material_id"] == acr.pk)["cut_speed_mm_s"] == 30

    assert client.patch(url, {"equipment_id": eq.pk, "laser_estimate_preset": "nope"}, format="json").status_code == 400
    assert (
        client.patch(
            url, {"equipment_id": eq.pk, "laser_estimate_overrides": {"setup_min": -1}}, format="json"
        ).status_code
        == 400
    )
    # Switching the machine type drops the old type's parameter and material overrides.
    resp = client.patch(url, {"equipment_id": eq.pk, "laser_estimate_preset": "fiber_laser"}, format="json")
    assert resp.status_code == 200
    eq.refresh_from_db()
    assert eq.laser_estimate_profile == {"preset": "fiber_laser"}
    assert LaserCutAnalysis.objects.count() == 0
