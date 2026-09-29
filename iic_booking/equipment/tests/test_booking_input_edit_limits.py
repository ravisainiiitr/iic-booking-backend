"""Editing user inputs after booking must respect the same numeric limits as booking creation."""

from __future__ import annotations

import pytest

from iic_booking.equipment.models import DynamicInputField, DynamicInputFieldType


def _fields(equipment, *, a_editable=True, b_editable=True):
    DynamicInputField.objects.create(
        equipment=equipment,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max_formula": "4*B"},
        editing_required=a_editable,
    )
    DynamicInputField.objects.create(
        equipment=equipment,
        field_key="B",
        field_label="Number of Slots",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=b_editable,
    )


def _patch(egs_factory, user, booking, values):
    return egs_factory.client_for(user).patch(
        f"/api/bookings/{booking.pk}/input-values/",
        {"input_values": values},
        format="json",
    )


@pytest.mark.django_db
def test_edit_rejects_field_a_above_formula_max(egs_factory):
    eq = egs_factory.equipment()
    _fields(eq)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 3, "B": 1})

    resp = _patch(egs_factory, owner, booking, {"A": 7, "B": 1})

    assert resp.status_code == 400
    assert "cannot be greater than 4" in resp.data["error"]
    booking.refresh_from_db()
    assert booking.input_values["A"] == 3


@pytest.mark.django_db
def test_edit_accepts_field_a_within_formula_max(egs_factory):
    eq = egs_factory.equipment()
    _fields(eq)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 3, "B": 1})

    resp = _patch(egs_factory, owner, booking, {"A": 4, "B": 1})

    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 4


@pytest.mark.django_db
def test_edit_rejects_lowering_b_below_what_existing_a_needs(egs_factory):
    eq = egs_factory.equipment()
    _fields(eq, a_editable=False)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 6, "B": 2})

    resp = _patch(egs_factory, owner, booking, {"A": 6, "B": 1})

    assert resp.status_code == 400
    assert "No. of Samples" in resp.data["error"]


@pytest.mark.django_db
def test_edit_rejects_static_max_and_min(egs_factory):
    eq = egs_factory.equipment()
    _fields(eq)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 1, "B": 1})

    assert _patch(egs_factory, owner, booking, {"A": 1, "B": 11}).status_code == 400
    assert _patch(egs_factory, owner, booking, {"A": 0, "B": 1}).status_code == 400


@pytest.mark.django_db
def test_comment_only_edit_still_saves_on_legacy_out_of_range_booking(egs_factory):
    eq = egs_factory.equipment()
    _fields(eq)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 7, "B": 1})

    resp = _patch(egs_factory, owner, booking, {"A": 7, "B": 1, "comments": "Please handle with care"})

    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["comments"] == "Please handle with care"
