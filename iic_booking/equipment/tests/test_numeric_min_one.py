"""Numeric user inputs cannot be 0 or negative: the minimum is at least 1 on every path that accepts input
values, unless the equipment set the field up for decimal or negative values. Values saved before the rule
(e.g. a 0) still display and are kept when an edit leaves them unchanged."""

from __future__ import annotations

import json

import pytest

from iic_booking.equipment.calculators import SAMPLE_SETS_KEY
from iic_booking.equipment.models import Booking, BookingInputTemplate, DynamicInputField, DynamicInputFieldType
from iic_booking.equipment.numeric_field_limits import (
    MIN_BELOW_ONE_MESSAGE,
    initial_numeric_value,
    normalize_numeric_field_config,
    numeric_field_allows_below_one,
    resolve_numeric_field_bounds,
)
from iic_booking.equipment.tests.test_sample_set_combined_limits import (  # noqa: F401 - fixture
    _book,
    _oic,
    _patch,
    _student_with_wallet,
    no_portal_lock,
)
from iic_booking.users.models.user_type import UserType

# --- resolver ---------------------------------------------------------------------------------------


def test_unconfigured_min_is_one():
    assert resolve_numeric_field_bounds(options=[], help_text="") == (1.0, 100.0, 1.0)
    assert resolve_numeric_field_bounds(options={"min": 0, "max": 5}) == (1.0, 5.0, 1.0)
    assert resolve_numeric_field_bounds(options=[], help_text="0\n10\n1") == (1.0, 10.0, 1.0)


def test_higher_configured_min_is_kept():
    assert resolve_numeric_field_bounds(options={"min": 2, "max": 8}) == (2.0, 8.0, 1.0)


def test_formula_max_below_one_is_raised_to_the_min():
    assert resolve_numeric_field_bounds(options={"max_formula": "B*4"}, formula_max=0) == (1.0, 1.0, 1.0)


@pytest.mark.parametrize(
    "options,help_text,default,expected_min",
    [
        ({"min": 0.1, "max": 5, "step": 0.1}, "", "0.1", 0.1),  # PXRD [A] scan speed (s/step)
        ([], "", "0.02", 0.0),  # PXRD [B] step size: no limits configured, default 0.02 degree
        ({"min": -7, "max": 16.3, "step": 0.1}, "", "-7", -7.0),  # UPS starting range (eV)
        ({"allow_negative": True, "max": 10}, "", "", -10.0),
        ({"step": 0.5, "max": 10}, "", "", 0.0),
    ],
)
def test_decimal_and_negative_fields_keep_their_minimum(options, help_text, default, expected_min):
    assert numeric_field_allows_below_one(options=options, help_text=help_text, default_value=default)
    assert resolve_numeric_field_bounds(options=options, help_text=help_text, default_value=default)[0] == expected_min


@pytest.mark.parametrize("default", ["0", "", None, "1", "16.3"])
def test_zero_or_whole_defaults_do_not_lift_the_minimum(default):
    assert not numeric_field_allows_below_one(options=[], help_text="", default_value=default)


def test_unset_step_follows_a_fractional_default():
    assert resolve_numeric_field_bounds(options=[], default_value="0.02")[2] == pytest.approx(0.01)
    assert resolve_numeric_field_bounds(options=[], default_value="16.3")[2] == pytest.approx(0.1)
    assert resolve_numeric_field_bounds(options=[], default_value="5")[2] == 1.0
    assert resolve_numeric_field_bounds(options={"step": 0.5}, default_value="0.02")[2] == 0.5


def test_floor_can_be_turned_off():
    assert resolve_numeric_field_bounds(options=[], apply_min_floor=False)[0] == 0.0


def test_initial_value():
    assert initial_numeric_value(options=[], default_value="0", is_required=False) == ""
    assert initial_numeric_value(options=[], default_value="0", is_required=True) == "1"
    assert initial_numeric_value(options=[], default_value="4") == "4"
    assert initial_numeric_value(options=[], default_value="", is_required=True) == "1"
    assert initial_numeric_value(options=[], default_value="0.02") == "0.02"


# --- equipment form ---------------------------------------------------------------------------------


@pytest.mark.parametrize("options", [{"min": 0}, {"min": "0", "max": 5}, {"min": 0.5}])
def test_equipment_form_rejects_min_below_one(options):
    with pytest.raises(ValueError) as exc:
        normalize_numeric_field_config(options, "")
    assert str(exc.value) == MIN_BELOW_ONE_MESSAGE


def test_equipment_form_rejects_help_text_min_zero():
    with pytest.raises(ValueError):
        normalize_numeric_field_config([], "0\n10\n1")


@pytest.mark.parametrize(
    "options",
    [
        {"min": 1, "max": 5},
        {"min": 0, "step": 0.01},
        {"min": 0.1, "max": 5, "step": 0.1},
        {"min": -7, "max": 16.3, "step": 0.1},
        {"min": 0, "allow_negative": True},
        {"max": 5},
    ],
)
def test_equipment_form_accepts(options):
    normalize_numeric_field_config(options, "")


# --- booking paths ----------------------------------------------------------------------------------


def _equipment(egs_factory, **kwargs):
    """A = No. of Samples (max 4, no min); B = No. of Slots (min 2); D = optional magnetic moment (default 0);
    E = step size (decimal, default 0.02)."""
    eq = egs_factory.equipment(time_formula="30", **kwargs)
    rows = [
        ("A", "No. of Samples", {"max": 4}, "1", True, True),
        ("B", "No. of Slots", {"min": 2, "max": 6}, "2", True, True),
        ("D", "Magnetic Moment (emu)", [], "0", False, True),
        ("E", "Step Size (Degree)", [], "0.02", True, False),
    ]
    for key, label, options, default, required, editable in rows:
        DynamicInputField.objects.create(
            equipment=eq, field_key=key, field_label=label, field_type=DynamicInputFieldType.NUMERIC,
            options=options, default_value=default, is_required=required, editing_required=editable,
        )
    return eq


VALID = {"A": 1, "B": 2, "E": 0.02}


@pytest.mark.django_db
@pytest.mark.parametrize("value", [0, -1, "0"])
def test_calculate_rejects_zero_and_negative(egs_factory, value):
    eq = _equipment(egs_factory)
    resp = egs_factory.client_for(egs_factory.student()).get(f"/api/equipments/{eq.pk}/calculate/", {**VALID, "A": value})
    assert resp.status_code == 400
    assert resp.data["error"] == "No. of Samples cannot be less than 1."


@pytest.mark.django_db
def test_calculate_keeps_configured_min_max_messages_and_decimals(egs_factory):
    eq = _equipment(egs_factory)
    client = egs_factory.client_for(egs_factory.student())
    url = f"/api/equipments/{eq.pk}/calculate/"

    assert client.get(url, {**VALID, "B": 1}).data["error"] == "No. of Slots cannot be less than 2."
    assert client.get(url, {**VALID, "A": 5}).data["error"] == "No. of Samples cannot be greater than 4."
    ok = client.get(url, VALID)
    assert ok.status_code == 200, ok.data
    blank_optional = client.get(url, {**VALID, "D": ""})
    assert blank_optional.status_code == 200, blank_optional.data
    assert client.get(url, {**VALID, "D": 0}).data["error"] == "Magnetic Moment (emu) cannot be less than 1."


@pytest.mark.django_db
def test_calculate_rejects_zero_in_a_sample_set(egs_factory):
    eq = _equipment(egs_factory)
    resp = egs_factory.client_for(egs_factory.student()).get(
        f"/api/equipments/{eq.pk}/calculate/", {**VALID, "sample_sets": json.dumps([{"A": 0, "B": 2}])}
    )
    assert resp.status_code == 400
    assert resp.data["error"] == "Sample set 2: No. of Samples cannot be less than 1."


@pytest.mark.django_db
def test_create_rejects_zero(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    student = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(eq, egs_factory.future())

    resp = _book(egs_factory, student, eq, slot, {**VALID, "A": 0})

    assert resp.status_code == 400
    assert resp.data["error"] == "No. of Samples cannot be less than 1."
    assert not Booking.objects.filter(user=student).exists()


@pytest.mark.django_db
def test_key_that_is_not_numeric_for_the_user_type_is_not_floored(egs_factory):
    eq = egs_factory.equipment(time_formula="30")
    DynamicInputField.objects.create(
        equipment=eq, field_key="C", field_label="Count", field_type=DynamicInputFieldType.NUMERIC,
    )
    DynamicInputField.objects.create(
        equipment=eq, user_type=UserType.STUDENT, field_key="C", field_label="Gold coating",
        field_type=DynamicInputFieldType.RADIO, options=["0", "1"],
    )
    resp = egs_factory.client_for(egs_factory.student()).get(f"/api/equipments/{eq.pk}/calculate/", {"C": "0"})
    assert resp.status_code == 200, resp.data


# --- editing booked inputs --------------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_to_zero_is_rejected_for_user_and_oic(egs_factory):
    eq = _equipment(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={**VALID, "D": 3})

    for editor in (owner, _oic(egs_factory, eq)):
        for values in ({**VALID, "A": 0, "D": 3}, {**VALID, "D": 0}, {**VALID, "D": -2}):
            resp = _patch(egs_factory, editor, booking, values)
            assert resp.status_code == 400, (editor.user_type, values, resp.data)
            assert "cannot be less than 1." in resp.data["error"]
    booking.refresh_from_db()
    assert booking.input_values["D"] == 3


@pytest.mark.django_db
def test_legacy_zero_displays_and_is_kept_when_unchanged(egs_factory):
    eq = _equipment(egs_factory)
    owner = egs_factory.student()
    legacy = {**VALID, "D": 0, SAMPLE_SETS_KEY: [{"A": 1, "B": 2, "D": 0}]}
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values=legacy)

    listed = egs_factory.client_for(owner).get("/api/bookings/", {"booking_id": booking.pk, "limit": 1})
    assert listed.status_code == 200, listed.data
    assert listed.data["bookings"][0]["input_values"]["D"] == 0
    field_d = next(f for f in listed.data["bookings"][0]["input_fields"] if f["field_key"] == "D")
    assert "default_value" in field_d

    resp = _patch(egs_factory, owner, booking, {**legacy, "A": 2})
    assert resp.status_code == 200, resp.data
    resp = _patch(egs_factory, owner, booking, {**legacy, "A": 2, SAMPLE_SETS_KEY: [{"A": 2, "B": 2, "D": 0}]})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["D"] == 0
    assert booking.input_values["A"] == 2

    fixed = _patch(egs_factory, owner, booking, {**legacy, "A": 2, "D": 1})
    assert fixed.status_code == 200, fixed.data


# --- templates --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_template_rejects_zero_and_keeps_legacy_zero(egs_factory):
    eq = _equipment(egs_factory)
    user = egs_factory.student()
    client = egs_factory.client_for(user)
    url = "/api/booking-templates/"

    bad = client.post(url, {"equipment": eq.pk, "name": "Zero", "input_values": {**VALID, "A": "0"}}, format="json")
    assert bad.status_code == 400
    assert bad.data["error"] == "No. of Samples cannot be less than 1."
    bad_set = client.post(
        url,
        {"equipment": eq.pk, "name": "Zero set", "input_values": {**VALID, SAMPLE_SETS_KEY: [{"A": "-1"}]}},
        format="json",
    )
    assert bad_set.status_code == 400
    assert bad_set.data["error"] == "Sample set 2: No. of Samples cannot be less than 1."
    min_two = client.post(url, {"equipment": eq.pk, "name": "B1", "input_values": {**VALID, "B": "1"}}, format="json")
    assert min_two.status_code == 400
    assert min_two.data["error"] == "No. of Slots cannot be less than 2."
    over_max = client.post(url, {"equipment": eq.pk, "name": "A9", "input_values": {**VALID, "A": "9"}}, format="json")
    assert over_max.status_code == 201, over_max.data

    legacy = BookingInputTemplate.objects.create(user=user, equipment=eq, name="Legacy", input_values={**VALID, "D": 0})
    renamed = client.patch(f"{url}{legacy.pk}/", {"input_values": {**VALID, "D": 0, "A": 2}}, format="json")
    assert renamed.status_code == 200, renamed.data
    zeroed = client.patch(f"{url}{legacy.pk}/", {"input_values": {**VALID, "D": 0, "A": 0}}, format="json")
    assert zeroed.status_code == 400
    assert BookingInputTemplate.objects.get(pk=legacy.pk).input_values["A"] == 2


# --- proforma and urgent requests -------------------------------------------------------------------


@pytest.mark.django_db
def test_proforma_rejects_zero(egs_factory):
    eq = _equipment(egs_factory)
    client = egs_factory.client_for(egs_factory.student())

    resp = client.post(
        "/api/proforma-invoice/calculate/", {"items": [{"equipment_id": eq.pk, "input_values": {**VALID, "A": 0}}]},
        format="json",
    )

    assert resp.status_code == 400
    assert resp.data["error"] == f"{eq.code}: No. of Samples cannot be less than 1."


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value,message",
    [
        ("number_of_samples", 0, "Number of samples must be at least 1."),
        ("number_of_samples", -3, "Number of samples must be at least 1."),
        ("slots_requested", "0", "Number of slots must be at least 1."),
        ("slots_requested", "1.5", "Number of slots must be a whole number."),
    ],
)
def test_urgent_request_rejects_zero_counts(egs_factory, no_portal_lock, field, value, message):
    eq = _equipment(egs_factory)
    resp = egs_factory.client_for(egs_factory.student()).post(
        "/api/urgent-booking-requests/create/",
        {"equipment_id": eq.pk, "request_type": "NO_SLOT", "disclaimer_accepted": True, field: value},
        format="json",
    )
    assert resp.status_code == 400, resp.data
    assert resp.data["error"] == message
