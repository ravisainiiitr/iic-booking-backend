"""Compact "sets · samples" summary on booking list rows and lab dashboard rows."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from iic_booking.equipment.booking_sample_summary import (
    is_sample_count_label,
    sample_count_field_key,
    sample_summary,
)
from iic_booking.equipment.calculators import SAMPLE_SETS_KEY
from iic_booking.equipment.models import DynamicInputField, DynamicInputFieldType, EquipmentOperator
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.parametrize(
    "label",
    ["No. of Samples", "No of samples", "Number of samples", "Sample count", "Samples", "Samples (nos)", "Nos. of sample"],
)
def test_sample_count_labels(label):
    assert is_sample_count_label(label)


@pytest.mark.parametrize(
    "label",
    ["Sample type", "Number of slots per sample", "Elements per sample", "Sample Details", "Nano particle size", ""],
)
def test_other_labels_are_not_sample_counts(label):
    assert not is_sample_count_label(label)


def test_sample_count_field_must_be_numeric_and_first_in_key_order():
    fields = [
        {"field_key": "C", "field_label": "Number of samples", "field_type": "NUMERIC"},
        {"field_key": "A", "field_label": "No. of Samples", "field_type": "TEXT"},
        {"field_key": "B", "field_label": "Samples", "field_type": "NUMERIC"},
    ]
    assert sample_count_field_key(fields) == "B"
    assert sample_count_field_key([{"field_key": "A", "field_label": "Sample type", "field_type": "NUMERIC"}]) is None


def test_sample_summary_sums_every_sample_set():
    values = {"A": "3", "D": "Powder", SAMPLE_SETS_KEY: [{"A": 4}, {"A": "5"}, {}]}
    assert sample_summary(values, "A") == {"sets": 3, "samples": 12}
    assert sample_summary({"A": 2}, "A") == {"sets": 1, "samples": 2}
    assert sample_summary({"A": 2}, None) == {"sets": 1, "samples": None}
    assert sample_summary({"A": ""}, "A") == {"sets": 1, "samples": None}
    assert sample_summary(None, "A") == {"sets": 1, "samples": None}


def _equipment(egs_factory):
    eq = egs_factory.equipment()
    DynamicInputField.objects.create(
        equipment=eq, field_key="A", field_label="No. of Samples", field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 20},
    )
    DynamicInputField.objects.create(
        equipment=eq, field_key="B", field_label="Sample type", field_type=DynamicInputFieldType.TEXT,
    )
    return eq


def _operator(egs_factory, *equipment):
    lab = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)
    for eq in equipment:
        EquipmentOperator.objects.create(equipment=eq, operator=lab)
    return lab


@pytest.mark.django_db
def test_booking_list_rows_carry_sample_summary_without_per_row_queries(egs_factory):
    eq = _equipment(egs_factory)
    other = _equipment(egs_factory)
    owner = egs_factory.student()
    multi = egs_factory.booking(
        owner, eq, egs_factory.future(days=3), input_values={"A": 3, SAMPLE_SETS_KEY: [{"A": 4}, {"A": 5}]}
    )
    single = egs_factory.booking(owner, other, egs_factory.future(days=4), input_values={"A": 2, "B": "Film"})
    lab = _operator(egs_factory, eq, other)

    client = egs_factory.client_for(lab)
    resp = client.get("/api/bookings/", {"list_view": "1", "limit": 50})
    assert resp.status_code == 200, resp.data
    rows = {r["real_booking_id"]: r for r in resp.data["bookings"]}
    assert rows[multi.booking_id]["sample_summary"] == {"sets": 3, "samples": 12}
    assert rows[single.booking_id]["sample_summary"] == {"sets": 1, "samples": 2}

    with CaptureQueriesContext(connection) as ctx:
        client.get("/api/bookings/", {"list_view": "1", "limit": 50})
    field_queries = [q for q in ctx.captured_queries if "dynamicinputfield" in q["sql"].lower()]
    assert len(field_queries) == 1


@pytest.mark.django_db
def test_lab_dashboard_rows_carry_sample_summary(egs_factory):
    eq = _equipment(egs_factory)
    owner = egs_factory.student()
    start = egs_factory.future(days=1)
    booking = egs_factory.booking(owner, eq, start, input_values={"A": 1, SAMPLE_SETS_KEY: [{"A": 2}]})
    lab = _operator(egs_factory, eq)

    resp = egs_factory.client_for(lab).get(
        "/api/bookings/lab-operator-dashboard/", {"period": "week", "week_start": start.date().isoformat()}
    )
    assert resp.status_code == 200, resp.data
    rows = [b for day in resp.data["days"] for b in day["bookings"] if b["booking_id"] == booking.booking_id]
    assert rows, "booking should appear in the week calendar"
    assert rows[0]["sample_summary"] == {"sets": 2, "samples": 3}
