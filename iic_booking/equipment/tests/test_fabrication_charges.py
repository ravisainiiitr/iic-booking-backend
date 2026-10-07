"""Charge engine for 2D laser cutting and the 3D print quantity / own-material upgrades."""

from __future__ import annotations

from decimal import Decimal

import pytest

from iic_booking.equipment.calculators import ChargeCalculationEngine, TimeCalculationEngine
from iic_booking.equipment.fabrication import (
    BOOKED_MINUTES_KEY,
    OWN_MATERIAL_KEY,
    PARTS_KEY,
    PRINT_WEIGHT_KEY,
    apply_fabrication_to_input_values,
    build_laser_parts,
    inject_print_parts,
    strip_fabrication_keys,
)
from iic_booking.equipment.models import ChargeProfile

from .fabrication_helpers import (
    acrylic_3mm,
    laser_equipment,
    laser_part,
    print_equipment,
    print_material,
    print_part,
)


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _cp(equipment):
    return ChargeProfile.objects.get(equipment=equipment)


@pytest.mark.django_db
def test_laser_worked_example_charges_202_43_and_books_202(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory)
    owner = egs_factory.student()
    part = laser_part(eq, owner, acrylic_3mm(eq), quantity=5)

    inputs = {PARTS_KEY: build_laser_parts([part])}
    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 60)

    assert len(breakdown) == 1
    assert breakdown[0]["exact_amount"] == "202.43"
    assert "bracket" in breakdown[0]["description"]
    assert "× 5" in breakdown[0]["description"]
    # Booking totals are whole rupees (portal-wide rule); the part line keeps the exact amount.
    assert total == Decimal("202")


@pytest.mark.django_db
def test_laser_parts_are_summed_and_machine_time_added_only_for_positive_rate(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory)
    owner = egs_factory.student()
    acr = acrylic_3mm(eq)
    mdf = acrylic_3mm(eq, code="MDF-3", name="MDF 3 mm", rate="2505.60")
    parts = [
        laser_part(eq, owner, acr, quantity=5, name="a"),
        laser_part(eq, owner, mdf, width="300", height="300", quantity=2, name="b", sequence=1),
    ]
    inputs = {PARTS_KEY: build_laser_parts(parts)}
    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 120)
    # 202.43 + (90000*2/2972897.28)*2505.60 = 202.43 + 151.71
    assert [line["exact_amount"] for line in breakdown] == ["202.43", "151.71"]
    assert total == Decimal("354")

    cp = _cp(eq)
    cp.primary_unit_charge = Decimal("300.00")
    cp.save()
    total, breakdown = ChargeCalculationEngine.calculate_charge(cp, inputs, 120)
    assert breakdown[-1]["description"].startswith("120 min machine time")
    assert total == Decimal("954")


@pytest.mark.django_db
def test_laser_own_material_replaces_material_cost_once(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory, own_charge="250")
    owner = egs_factory.student()
    acr = acrylic_3mm(eq)
    parts = [laser_part(eq, owner, acr, quantity=5, name="a"), laser_part(eq, owner, acr, name="b", sequence=1)]
    inputs = {PARTS_KEY: build_laser_parts(parts), OWN_MATERIAL_KEY: True}
    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 60)
    assert total == Decimal("250")
    assert [line["description"] for line in breakdown] == ["Own material — fixed charge"]


@pytest.mark.django_db
def test_own_material_flag_is_ignored_when_the_equipment_has_no_fixed_charge(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory, own_charge=None)
    owner = egs_factory.student()
    inputs = {PARTS_KEY: build_laser_parts([laser_part(eq, owner, acrylic_3mm(eq), quantity=5)]), OWN_MATERIAL_KEY: True}
    total, _ = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 60)
    assert total == Decimal("202")


@pytest.mark.django_db
def test_laser_time_is_the_booked_duration(egs_factory, media_tmp):
    eq = laser_equipment(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), slot_count=2)
    laser_part(eq, owner, acrylic_3mm(eq), booking=booking)

    inputs = apply_fabrication_to_input_values(booking, {"D": "project"})
    assert inputs[BOOKED_MINUTES_KEY] == 120
    assert inputs[OWN_MATERIAL_KEY] is False
    assert TimeCalculationEngine.calculate_time(_cp(eq), inputs, slot_duration_minutes=60) == 120
    assert strip_fabrication_keys(inputs) == {"D": "project"}


@pytest.mark.django_db
def test_print_quantity_multiplies_material_and_time(egs_factory, media_tmp):
    eq = print_equipment(egs_factory, hourly_rate="60.00")
    owner = egs_factory.student()
    pla = print_material(eq)
    part = print_part(eq, owner, pla, weight="10.2", minutes=30, quantity=3)

    inputs = inject_print_parts({}, [part])
    assert inputs[PRINT_WEIGHT_KEY] == 33  # ceil(10.2) = 11 g each x 3
    assert "A" not in inputs
    assert inputs["C"] == 90
    assert inputs["B"] == "PLA-FDM"
    minutes = TimeCalculationEngine.calculate_time(_cp(eq), inputs, slot_duration_minutes=60)
    assert minutes == 90
    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, minutes)
    # 33 g x 1.44 = 47.52 ; 90 min x 60/h = 90 ; total 137.52 -> 138
    assert breakdown[0]["description"] == "gear: 11 g × 3 PLA (FDM) @ 1.44/g"
    assert total == Decimal("138")


@pytest.mark.django_db
def test_print_actual_weight_is_a_total_and_not_multiplied(egs_factory, media_tmp):
    eq = print_equipment(egs_factory)
    owner = egs_factory.student()
    part = print_part(eq, owner, print_material(eq), weight="10", minutes=30, quantity=3)
    part.actual_weight_grams = Decimal("40")
    part.actual_time_minutes = 100
    part.save()
    inputs = inject_print_parts({}, [part])
    assert (inputs[PRINT_WEIGHT_KEY], inputs["C"]) == (40, 100)
    # Actuals are the total of every copy, so Quantity Required does not multiply them either.
    inputs = inject_print_parts({}, [part], 4)
    assert (inputs[PRINT_WEIGHT_KEY], inputs["C"]) == (40, 100)


@pytest.mark.django_db
def test_print_own_material_uses_fixed_charge_plus_machine_time(egs_factory, media_tmp):
    eq = print_equipment(egs_factory, hourly_rate="60.00", own_charge="100")
    owner = egs_factory.student()
    part = print_part(eq, owner, print_material(eq), weight="10.2", minutes=30, quantity=3)
    inputs = inject_print_parts({}, [part])
    inputs[OWN_MATERIAL_KEY] = True
    total, breakdown = ChargeCalculationEngine.calculate_charge(_cp(eq), inputs, 90)
    assert breakdown[0]["description"] == "Own material (PLA (FDM)) — fixed charge"
    assert total == Decimal("190")


@pytest.mark.django_db
def test_partial_cancel_of_print_files_respects_quantity(egs_factory, media_tmp):
    from iic_booking.equipment.booking_cancellation import compute_partial_cancel_print_items

    eq = print_equipment(egs_factory)
    owner = egs_factory.student()
    pla = print_material(eq)
    booking = egs_factory.booking(owner, eq, egs_factory.future(), slot_count=3, total_charge="48.00")
    keep = print_part(eq, owner, pla, weight="10", minutes=30, quantity=2, name="keep", booking=booking)
    drop = print_part(eq, owner, pla, weight="10", minutes=30, quantity=1, name="drop", booking=booking, sequence=1)
    assert keep.booking_id == drop.booking_id == booking.pk

    plan = compute_partial_cancel_print_items(booking, [str(drop.id)])
    assert plan["new_input_values"]["A"] == 20
    assert plan["new_time_minutes"] == 60
    assert plan["refund_amount"] > 0
