"""Own material offered by default, the own-sheet size worked out from the drawing, and the ₹0 own-material charge."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from iic_booking.equipment.calculators import ChargeCalculationEngine
from iic_booking.equipment.fabrication import (
    OWN_MATERIAL_KEY,
    PARTS_KEY,
    build_laser_parts,
    inject_print_parts,
    own_material_available,
)
from iic_booking.equipment.laser_cut_service import OWN_SHEET_MARGIN_MM, own_sheet_size
from iic_booking.equipment.models import ChargeProfile, Equipment, EquipmentProfileType

from .fabrication_helpers import (
    acrylic_3mm,
    dxf_bytes,
    laser_equipment,
    laser_part,
    print_equipment,
    print_material,
    print_part,
)


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    return tmp_path


def _cp(eq):
    return ChargeProfile.objects.get(equipment=eq)


# --------------------------------------------------------------------------- own sheet size helper


def test_own_sheet_adds_the_margin_on_every_side_and_rounds_up_to_the_next_mm():
    size = own_sheet_size("200", "100.2")
    assert OWN_SHEET_MARGIN_MM == Decimal("5")
    assert (size.width_mm, size.height_mm, size.rotated) == (Decimal("210"), Decimal("111"), False)
    assert size.as_dict() == {"width_mm": "210", "height_mm": "111", "rotated": False}


def test_own_sheet_uses_the_given_margin_and_none_without_a_size():
    assert own_sheet_size("50.01", "20", margin_mm="0").as_dict()["width_mm"] == "51"
    assert own_sheet_size("50", "20", margin_mm="2.5").as_dict() == {"width_mm": "55", "height_mm": "25", "rotated": False}
    assert own_sheet_size(None, "20") is None
    assert own_sheet_size("0", "20") is None


def test_own_sheet_is_turned_when_that_overflows_the_bed_less():
    # 1300 tall part on a 2438 x 1219 bed: upright it overflows by 91 mm, turned it fits.
    size = own_sheet_size("400", "1300", bed=("2438.4", "1219.2"))
    assert (size.width_mm, size.height_mm, size.rotated) == (Decimal("1310"), Decimal("410"), True)


def test_own_sheet_keeps_its_orientation_when_it_fits_the_bed_either_way():
    size = own_sheet_size("100", "300", bed=("2438.4", "1219.2"))
    assert (size.width_mm, size.height_mm, size.rotated) == (Decimal("110"), Decimal("310"), False)


def test_own_sheet_picks_the_smallest_standard_size_it_fits_on():
    standard = [("600", "300"), ("297", "210"), ("420", "297")]
    assert own_sheet_size("280", "190", standard_sizes=standard).as_dict()["width_mm"] == "297"
    # 290 x 200 part + margins = 300 x 210 needs the 420 x 297 sheet; turned to the part's orientation.
    assert own_sheet_size("200", "290", standard_sizes=standard).as_dict() == {
        "width_mm": "297",
        "height_mm": "420",
        "rotated": False,
    }
    # Larger than every standard size: rounded mm.
    assert own_sheet_size("700", "100", standard_sizes=standard).as_dict()["width_mm"] == "710"


# --------------------------------------------------------------------------- default availability


@pytest.mark.django_db
def test_new_fabrication_equipment_offers_own_material_at_no_material_charge(egs_factory):
    eq = egs_factory.equipment(with_profile=False, profile_type=EquipmentProfileType.LASER_CUT_2D)
    assert eq.own_material_fixed_charge == Decimal("0.00")
    assert own_material_available(eq)
    other = egs_factory.equipment(with_profile=False)
    assert not own_material_available(other)


@pytest.mark.django_db
def test_enable_command_dry_run_then_apply(egs_factory):
    blank = laser_equipment(egs_factory, own_charge=None)
    priced = print_equipment(egs_factory, own_charge="150")
    plain = egs_factory.equipment(with_profile=False)
    Equipment.objects.filter(pk=plain.pk).update(own_material_fixed_charge=None)

    out = StringIO()
    call_command("enable_fabrication_own_material", stdout=out)
    text = out.getvalue()
    assert "MODE=dry-run" in text
    assert f"id={blank.pk} code={blank.code}" in text and "to_enable" in text
    assert "to_enable=1 already_enabled=1" in text
    blank.refresh_from_db()
    assert blank.own_material_fixed_charge is None

    out = StringIO()
    call_command("enable_fabrication_own_material", "--apply", stdout=out)
    assert "enabled=1 already_enabled=1" in out.getvalue()
    blank.refresh_from_db()
    priced.refresh_from_db()
    plain.refresh_from_db()
    assert blank.own_material_fixed_charge == Decimal("0.00")
    assert priced.own_material_fixed_charge == Decimal("150.00")
    assert plain.own_material_fixed_charge is None

    out = StringIO()
    call_command("enable_fabrication_own_material", "--apply", stdout=out)
    assert "enabled=0 already_enabled=2" in out.getvalue()


# --------------------------------------------------------------------------- charges with own material at ₹0


@pytest.mark.django_db
def test_laser_own_material_at_zero_charges_no_material(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory, own_charge="0")
    owner = egs_factory.student()
    inputs = {PARTS_KEY: build_laser_parts([laser_part(eq, owner, acrylic_3mm(eq), quantity=5)]), OWN_MATERIAL_KEY: True}
    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 60)
    assert total == Decimal("0")
    assert [line["description"] for line in breakdown] == ["Own material — no material charge"]


@pytest.mark.django_db
def test_print_own_material_at_zero_still_charges_machine_time(egs_factory, media_tmp):
    eq = print_equipment(egs_factory, hourly_rate="60.00", own_charge="0")
    owner = egs_factory.student()
    inputs = inject_print_parts({}, [print_part(eq, owner, print_material(eq), weight="10.2", minutes=30, quantity=3)])
    inputs[OWN_MATERIAL_KEY] = True
    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 90)
    assert breakdown[0]["description"] == "Own material (PLA (FDM)) — no material charge"
    assert total == Decimal("90")


# --------------------------------------------------------------------------- own sheet on parts


@pytest.mark.django_db
def test_parts_carry_the_own_sheet_size_from_the_drawing_or_the_user(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory, own_charge="0")
    owner = egs_factory.student()
    part = laser_part(eq, owner, acrylic_3mm(eq), width="200", height="100")
    row = build_laser_parts([part])[0]
    assert (row["own_sheet_width_mm"], row["own_sheet_height_mm"]) == ("210", "110")

    part.own_sheet_width_mm, part.own_sheet_height_mm = Decimal("300"), Decimal("200")
    part.save()
    row = build_laser_parts([part])[0]
    assert (row["own_sheet_width_mm"], row["own_sheet_height_mm"]) == ("300", "200")


@pytest.mark.django_db
def test_user_can_enter_and_reset_the_own_sheet_size(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory, own_charge="0")
    acr = acrylic_3mm(eq)
    student = egs_factory.student()
    client = egs_factory.client_for(student)
    resp = client.post(
        f"/api/equipments/{eq.pk}/analyze-dxf/",
        {"file": SimpleUploadedFile("plate.dxf", dxf_bytes(rects=((0, 0, 200, 100),))), "material_id": acr.pk},
        format="multipart",
    )
    assert resp.status_code == 200, resp.data
    item = resp.data["items"][0]
    assert item["own_sheet_suggested"] == {"width_mm": "210", "height_mm": "110", "rotated": False}
    assert item["own_sheet_width_mm"] is None
    url = f"/api/laser-cut-analyses/{item['id']}/"

    resp = client.patch(url, {"own_sheet_width_mm": "300", "own_sheet_height_mm": 250.04}, format="json")
    assert resp.status_code == 200, resp.data
    assert (Decimal(resp.data["own_sheet_width_mm"]), Decimal(resp.data["own_sheet_height_mm"])) == (
        Decimal("300"),
        Decimal("250.1"),
    )

    # Turned is fine; smaller than the part is not.
    assert client.patch(url, {"own_sheet_width_mm": 100, "own_sheet_height_mm": 200}, format="json").status_code == 200
    resp = client.patch(url, {"own_sheet_width_mm": 150, "own_sheet_height_mm": 90}, format="json")
    assert resp.status_code == 400
    assert "at least" in resp.data["error"]
    assert client.patch(url, {"own_sheet_width_mm": "abc", "own_sheet_height_mm": 90}, format="json").status_code == 400

    resp = client.patch(url, {"own_sheet_width_mm": None, "own_sheet_height_mm": ""}, format="json")
    assert resp.status_code == 200
    assert resp.data["own_sheet_width_mm"] is None and resp.data["own_sheet_height_mm"] is None
