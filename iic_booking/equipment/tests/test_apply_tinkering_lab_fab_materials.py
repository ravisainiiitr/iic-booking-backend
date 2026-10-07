"""apply_tinkering_lab_fab_materials: CNC Machine Tools of one department switch to 2D laser cutting and support the
enabled laser sheets; its 3D printers support the enabled 3D print materials (one per code, own material first)."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from iic_booking.equipment.calculators import ChargeCalculationEngine, TimeCalculationEngine
from iic_booking.equipment.fabrication import PARTS_KEY
from iic_booking.equipment.fabrication_material_support import bookable_materials, supported_materials
from iic_booking.equipment.management.commands.apply_tinkering_lab_fab_materials import (
    DEFAULT_DEPARTMENT,
    SLOTS_TIME_FORMULA,
)
from iic_booking.equipment.models import (
    ChargeProfile,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentCategory,
    EquipmentProfileType,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType

from .fabrication_helpers import acrylic_3mm, laser_equipment, print_equipment, print_material

pytestmark = pytest.mark.django_db

HOUR = EquipmentProfileType.HOUR
SAMPLE = EquipmentProfileType.SAMPLE
LASER = EquipmentProfileType.LASER_CUT_2D


def _run(*args) -> str:
    out = StringIO()
    call_command("apply_tinkering_lab_fab_materials", *args, stdout=out)
    return out.getvalue()


def _ids(qs):
    return sorted(qs.values_list("pk", flat=True))


@pytest.fixture
def lab(egs_factory):
    dept = Department.objects.create(
        name=DEFAULT_DEPARTMENT, code="TLX", equipment_booking_enabled=True, equipment_visibility_enabled=True
    )
    cnc_cat = EquipmentCategory.objects.create(name="CNC Machine Tools")
    printer_cat = EquipmentCategory.objects.create(name="3D Printers")
    other_cat = EquipmentCategory.objects.create(name="Scanner")

    def cnc(profile=HOUR, time_formula="", department=dept, category=cnc_cat):
        eq = egs_factory.equipment(
            with_profile=False, profile_type=profile, category=category, internal_department=department,
            slot_duration_minutes=480,
        )
        for pricing in ("standard", "discounted"):
            ChargeProfile.objects.create(
                equipment=eq, user_type=UserType.STUDENT, pricing_profile=pricing, profile_type=profile,
                time_formula=time_formula, primary_unit_charge=Decimal("0.00"),
            )
        DynamicInputField.objects.create(
            equipment=eq, user_type=UserType.STUDENT, field_key="A", field_label="Hours",
            field_type=DynamicInputFieldType.NUMERIC, is_required=True,
        )
        if profile == HOUR:
            DynamicInputField.objects.create(
                equipment=eq, user_type=UserType.STUDENT, field_key="B", field_label="Slots",
                field_type=DynamicInputFieldType.NUMERIC, is_required=True,
            )
        return eq

    def printer(department=dept):
        return print_equipment(egs_factory, category=printer_cat, internal_department=department)

    # Master-list owners outside the lab (like the IIC test equipment in production).
    sheet_owner = laser_equipment(egs_factory)
    print_owner = print_equipment(egs_factory)
    return {
        "dept": dept, "cnc": cnc, "printer": printer, "other_cat": other_cat,
        "sheet_owner": sheet_owner, "print_owner": print_owner,
    }


def test_only_the_departments_cnc_equipment_switches_and_gets_all_enabled_sheets(lab, egs_factory):
    owner = lab["sheet_owner"]
    acr3 = acrylic_3mm(owner)
    ms1 = acrylic_3mm(owner, code="MS-1", rate="6026.40")
    off = acrylic_3mm(owner, code="OLD-1", is_active=False)

    hour_cnc = lab["cnc"]()
    sample_cnc = lab["cnc"](profile=SAMPLE, time_formula="A*480")
    scanner = lab["cnc"](category=lab["other_cat"])
    other_dept_cnc = lab["cnc"](department=egs_factory.department)

    out = _run("--apply")
    assert "APPLIED." in out

    for eq in (hour_cnc, sample_cnc):
        eq.refresh_from_db()
        assert eq.profile_type == LASER
        assert set(ChargeProfile.objects.filter(equipment=eq).values_list("profile_type", flat=True)) == {LASER}
        assert _ids(supported_materials(eq)) == sorted([acr3.pk, ms1.pk])
        assert off.pk not in _ids(supported_materials(eq))
    for eq in (scanner, other_dept_cnc):
        eq.refresh_from_db()
        assert eq.profile_type == HOUR
        assert set(ChargeProfile.objects.filter(equipment=eq).values_list("profile_type", flat=True)) == {HOUR}
        assert not eq.supported_laser_sheet_materials.exists()
    # The sheets stay supported by the equipment they were added for.
    assert _ids(supported_materials(owner)) == sorted([acr3.pk, ms1.pk, off.pk])


def test_profile_switch_keeps_slot_count_and_the_laser_charge_computes(lab):
    sheet = acrylic_3mm(lab["sheet_owner"])
    hour_cnc = lab["cnc"]()
    sample_cnc = lab["cnc"](profile=SAMPLE, time_formula="A*480")
    _run("--apply")

    hour_cp = ChargeProfile.objects.get(equipment=hour_cnc, pricing_profile="standard")
    assert hour_cp.time_formula == SLOTS_TIME_FORMULA
    assert TimeCalculationEngine.calculate_time(hour_cp, {"B": 3}, 480) == 3 * 480
    assert TimeCalculationEngine.calculate_time(hour_cp, {}, 480) == 480
    sample_cp = ChargeProfile.objects.get(equipment=sample_cnc, pricing_profile="standard")
    assert sample_cp.time_formula == "A*480"
    assert TimeCalculationEngine.calculate_time(sample_cp, {"A": 2}, 480) == 960

    part = {
        "name": "bracket", "quantity": 2, "area_mm2": "20000", "material_code": sheet.code,
        "sheet_width_mm": str(sheet.sheet_width_mm), "sheet_height_mm": str(sheet.sheet_height_mm),
        "sheet_rate": str(sheet.sheet_rate),
    }
    total, breakdown = ChargeCalculationEngine.calculate_charge(hour_cp, {PARTS_KEY: [part]}, 480)
    assert total > 0
    assert breakdown and "bracket" in breakdown[0]["description"]
    hour_cnc.refresh_from_db()
    assert list(bookable_materials(hour_cnc)) == [sheet]


def test_printers_get_one_material_per_code_keeping_their_own(lab, egs_factory):
    owner = lab["print_owner"]
    p_a, p_b, p_c = lab["printer"](), lab["printer"](), lab["printer"]()
    a_m1 = print_material(p_a, code="M1", price="1.5000")
    b_m1 = print_material(p_b, code="M1", price="16.2000")
    b_m2 = print_material(p_b, code="M2", price="1.5000")
    c_c1 = print_material(p_c, code="C1", price="2.5000")
    c_off = print_material(p_c, code="OLD", price="9.0000")
    c_off.is_active = False
    c_off.save(update_fields=["is_active"])
    pla = print_material(owner, code="PLA-FDM", price="1.4400")
    pla_dup = print_material(print_equipment(egs_factory), code="pla-fdm")

    out = _run("--apply")

    assert _ids(supported_materials(p_a)) == sorted([a_m1.pk, b_m2.pk, c_c1.pk, pla.pk])
    assert _ids(supported_materials(p_b)) == sorted([b_m1.pk, b_m2.pk, c_c1.pk, pla.pk])
    # P_c owns no M1: the lowest-id enabled M1 is taken and reported. Its disabled own row stays linked.
    assert _ids(supported_materials(p_c)) == sorted([a_m1.pk, b_m2.pk, c_c1.pk, c_off.pk, pla.pk])
    assert pla_dup.pk not in _ids(supported_materials(p_a))
    assert f"choice M1: several enabled rows, took the lowest id {a_m1.pk}" in out
    assert f"skip duplicates of M1: kept #{b_m1.pk}" in out
    assert f"price {p_c.code}#{p_c.pk} M1#{a_m1.pk}: 1.5000/g (from {p_a.code}#{p_a.pk})" in out
    assert f"price {p_b.code}#{p_b.pk} M1#{b_m1.pk}: 16.2000/g (own)" in out


def test_disabled_material_of_another_equipment_gives_way_to_an_enabled_one(lab):
    p_a, p_b = lab["printer"](), lab["printer"]()
    stale = print_material(p_b, code="PETG")
    stale.is_active = False
    stale.save(update_fields=["is_active"])
    p_a.supported_print_materials.add(stale)
    fresh = print_material(lab["print_owner"], code="PETG")

    out = _run("--apply")

    assert stale.pk not in _ids(supported_materials(p_a))
    assert fresh.pk in _ids(supported_materials(p_a))
    assert f"unlink PETG#{stale.pk}" in out
    # The owner keeps its own (disabled) row.
    assert stale.pk in _ids(supported_materials(p_b))


def test_second_run_changes_nothing(lab):
    acrylic_3mm(lab["sheet_owner"])
    cnc = lab["cnc"]()
    printer = lab["printer"]()
    print_material(printer, code="M1")
    print_material(lab["print_owner"], code="PLA-FDM")
    _run("--apply")
    profiles = list(ChargeProfile.objects.filter(equipment=cnc).values_list("pk", "profile_type", "time_formula", "updated_at"))
    links = (_ids(supported_materials(cnc)), _ids(supported_materials(printer)))

    out = _run("--apply")

    assert "charge profile" not in out and "equipment profile" not in out
    assert out.count("materials already complete") == 2
    assert list(ChargeProfile.objects.filter(equipment=cnc).values_list("pk", "profile_type", "time_formula", "updated_at")) == profiles
    assert (_ids(supported_materials(cnc)), _ids(supported_materials(printer))) == links


def test_dry_run_saves_nothing(lab):
    acrylic_3mm(lab["sheet_owner"])
    cnc = lab["cnc"]()
    printer = lab["printer"]()
    print_material(lab["print_owner"], code="PLA-FDM")

    out = _run()

    assert "DRY RUN: nothing was saved." in out
    assert "equipment profile HOUR -> LASER_CUT_2D" in out
    cnc.refresh_from_db()
    assert cnc.profile_type == HOUR
    assert not cnc.supported_laser_sheet_materials.exists()
    assert not printer.supported_print_materials.exists()


def test_upcoming_booking_keeps_its_stored_charge_and_can_hold_the_switch(lab, egs_factory):
    acrylic_3mm(lab["sheet_owner"])
    cnc = lab["cnc"]()
    booking = egs_factory.booking(
        egs_factory.student(), cnc, egs_factory.future(days=2), input_values={"A": 1, "B": 1}, total_charge="250.00"
    )

    out = _run("--apply", "--skip-equipment-with-upcoming")
    assert f"upcoming booking {booking.pk} status=BOOKED" in out
    assert "SKIP: has upcoming bookings" in out
    cnc.refresh_from_db()
    assert cnc.profile_type == HOUR

    _run("--apply")
    cnc.refresh_from_db()
    booking.refresh_from_db()
    assert cnc.profile_type == LASER
    assert booking.total_charge == Decimal("250.00")
    assert booking.input_values == {"A": 1, "B": 1}
    assert booking.status == "BOOKED"
