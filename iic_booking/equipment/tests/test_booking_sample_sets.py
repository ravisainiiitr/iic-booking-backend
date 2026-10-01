"""Several samples with different parameters in one booking: time and charge add up per sample set."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from iic_booking.equipment.calculators import (
    SAMPLE_SETS_KEY,
    ChargeCalculationEngine,
    TimeCalculationEngine,
    build_safe_input_values_for_charge_calculation,
)
from iic_booking.equipment.models import ChargeProfile, DynamicInputField, DynamicInputFieldType


def _equipment(egs_factory, **kwargs):
    eq = egs_factory.equipment(time_formula="A*30", **kwargs)
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=True,
    )
    return eq


@pytest.mark.django_db
def test_time_and_charge_add_up_across_sample_sets(egs_factory):
    eq = _equipment(egs_factory)
    profile = ChargeProfile.objects.get(equipment=eq)

    single = build_safe_input_values_for_charge_calculation({"A": 2}, equipment=eq)
    single_time = TimeCalculationEngine.calculate_time(profile, single, 60)
    single_charge, _ = ChargeCalculationEngine.calculate_charge(profile, single, single_time)

    combined = build_safe_input_values_for_charge_calculation(
        {"A": 2, SAMPLE_SETS_KEY: [{"A": "4"}, {}]}, equipment=eq
    )
    assert combined[SAMPLE_SETS_KEY] == [{"A": 4}]
    time = TimeCalculationEngine.calculate_time(profile, combined, 60)
    charge, breakdown = ChargeCalculationEngine.calculate_charge(profile, combined, time)

    four = build_safe_input_values_for_charge_calculation({"A": 4}, equipment=eq)
    four_time = TimeCalculationEngine.calculate_time(profile, four, 60)
    four_charge, _ = ChargeCalculationEngine.calculate_charge(profile, four, four_time)

    assert time == single_time + four_time
    assert charge == single_charge + four_charge
    assert any(str(line.get("description", "")).startswith("Sample set 2:") for line in breakdown)


@pytest.mark.django_db
def test_charge_estimate_accepts_sample_sets_and_validates_them(egs_factory):
    eq = _equipment(egs_factory)
    student = egs_factory.student()
    client = egs_factory.client_for(student)
    url = f"/api/equipments/{eq.pk}/calculate/"

    base = client.get(url, {"A": 2})
    assert base.status_code == 200, base.data
    combined = client.get(url, {"A": 2, "sample_sets": json.dumps([{"A": 2}])})
    assert combined.status_code == 200, combined.data
    assert int(combined.data["total_time_minutes"]) == 2 * int(base.data["total_time_minutes"])

    too_many = client.get(url, {"A": 2, "sample_sets": json.dumps([{"A": 50}])})
    assert too_many.status_code == 400
    assert "Sample set 2" in too_many.data["error"]


@pytest.mark.django_db
def test_user_can_add_sample_set_when_editing_inputs(egs_factory):
    eq = _equipment(egs_factory, enable_charge_recalculation=True)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 2}, total_charge="10.00")

    resp = egs_factory.client_for(owner).patch(
        f"/api/bookings/{booking.pk}/input-values/",
        {"input_values": {"A": 2, SAMPLE_SETS_KEY: [{"A": 2}]}},
        format="json",
    )

    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values[SAMPLE_SETS_KEY] == [{"A": 2}]
    assert booking.total_time_minutes == 120
    assert booking.total_charge == Decimal("20.00")
