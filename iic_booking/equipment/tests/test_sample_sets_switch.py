"""Per-equipment switch "Allow samples with different parameters" (Equipment.allow_multiple_sample_sets)."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.calculators import SAMPLE_SETS_KEY, ChargeCalculationEngine, TimeCalculationEngine
from iic_booking.equipment.models import Booking, BookingInputTemplate, Equipment
from iic_booking.equipment.sample_set_limits import SAMPLE_SETS_DISABLED_MESSAGE
from iic_booking.equipment.serializers import EquipmentAdminWriteSerializer
from iic_booking.equipment.tests.test_sample_set_combined_limits import (
    _book,
    _equipment,
    _oic,
    _patch,
    _student_with_wallet,
    no_portal_lock,  # noqa: F401 - pytest fixture
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _switched_off(egs_factory, **kwargs):
    return _equipment(egs_factory, allow_multiple_sample_sets=False, **kwargs)


# --- default and configuration ---------------------------------------------------------------------


@pytest.mark.django_db
def test_switch_defaults_to_on(egs_factory):
    eq = egs_factory.equipment()
    assert eq.allow_multiple_sample_sets is True
    assert Equipment._meta.get_field("allow_multiple_sample_sets").default is True


@pytest.mark.django_db
def test_equipment_api_exposes_the_switch(egs_factory):
    on = _equipment(egs_factory)
    off = _switched_off(egs_factory)
    client = egs_factory.client_for(egs_factory.student())

    assert client.get(f"/api/equipments/{on.pk}/").data["allow_multiple_sample_sets"] is True
    assert client.get(f"/api/equipments/{off.pk}/").data["allow_multiple_sample_sets"] is False


@pytest.mark.django_db
def test_booking_payload_carries_the_switch(egs_factory):
    eq = _switched_off(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 1, "B": 1})

    resp = egs_factory.client_for(owner).get("/api/bookings/", {"booking_id": booking.pk, "limit": 1})

    assert resp.status_code == 200, resp.data
    assert resp.data["bookings"][0]["equipment_allow_multiple_sample_sets"] is False


def _write(user, instance, value):
    request = SimpleNamespace(user=user)
    return EquipmentAdminWriteSerializer(
        instance, data={"allow_multiple_sample_sets": value}, partial=True, context={"request": request}
    )


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["admin", "superuser"])
def test_main_admin_can_change_the_switch(egs_factory, role):
    eq = _equipment(egs_factory)
    user = UserFactory(user_type=UserType.ADMIN, is_superuser=role == "superuser")

    serializer = _write(user, eq, False)
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    eq.refresh_from_db()
    assert eq.allow_multiple_sample_sets is False


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["oic", "dept_admin", "operator"])
def test_others_cannot_change_the_switch_but_may_resubmit_it(egs_factory, role):
    eq = _equipment(egs_factory)
    if role == "oic":
        user = _oic(egs_factory, eq)
    elif role == "dept_admin":
        user = UserFactory(user_type=UserType.DEPT_ADMIN, department=egs_factory.department, admin_approved=True)
    else:
        user = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)

    changed = _write(user, eq, False)
    assert not changed.is_valid()
    assert "Only the main administrator" in str(changed.errors["allow_multiple_sample_sets"][0])

    unchanged = _write(user, eq, True)
    assert unchanged.is_valid(), unchanged.errors
    unchanged.save()
    eq.refresh_from_db()
    assert eq.allow_multiple_sample_sets is True


@pytest.mark.django_db
def test_main_admin_turns_the_switch_off_through_the_admin_api(egs_factory):
    eq = _equipment(egs_factory)
    client = APIClient()
    client.force_authenticate(user=UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True, admin_approved=True))

    resp = client.patch(f"/api/admin/equipment/{eq.pk}/", {"allow_multiple_sample_sets": False}, format="json")

    assert resp.status_code == 200, getattr(resp, "data", resp.content[:2000])
    assert resp.data["allow_multiple_sample_sets"] is False
    eq.refresh_from_db()
    assert eq.allow_multiple_sample_sets is False


def test_django_admin_form_shows_the_switch_ticked_by_default():
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    ma = site._registry[Equipment]
    req = RequestFactory().get("/admin/equipment/equipment/add/")
    req.user = SimpleNamespace(is_superuser=True, is_staff=True, is_active=True, has_perm=lambda *a, **k: True)
    fields = [f for _name, opts in ma.get_fieldsets(req, None) for f in opts["fields"]]
    assert "allow_multiple_sample_sets" in fields

    form = ma.form()
    field = form.fields["allow_multiple_sample_sets"]
    assert form.get_initial_for_field(field, "allow_multiple_sample_sets") is True
    assert field.label == "Allow samples with different parameters"


# --- switched off: new sets are rejected -----------------------------------------------------------


@pytest.mark.django_db
def test_create_with_extra_sets_rejected_when_off(egs_factory, no_portal_lock):  # noqa: F811
    eq = _switched_off(egs_factory)
    student = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(eq, egs_factory.future())

    resp = _book(egs_factory, student, eq, slot, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]})

    assert resp.status_code == 400
    assert resp.data["error"] == SAMPLE_SETS_DISABLED_MESSAGE
    assert not Booking.objects.filter(user=student).exists()

    single = _book(egs_factory, student, eq, slot, {"A": 1, "B": 1})
    assert single.status_code == 201, single.data


@pytest.mark.django_db
def test_charge_estimate_with_extra_sets_rejected_when_off(egs_factory):
    eq = _switched_off(egs_factory)
    client = egs_factory.client_for(egs_factory.student())
    url = f"/api/equipments/{eq.pk}/calculate/"

    resp = client.get(url, {"A": 1, "B": 1, "sample_sets": json.dumps([{"A": 1, "B": 1}])})
    assert resp.status_code == 400
    assert resp.data["error"] == SAMPLE_SETS_DISABLED_MESSAGE

    # Empty sets are ignored, as before.
    assert client.get(url, {"A": 1, "B": 1, "sample_sets": json.dumps([{}])}).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["oic", "admin"])
def test_edit_cannot_add_a_set_when_off(egs_factory, role):
    eq = _switched_off(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 1, "B": 1})
    editor = _oic(egs_factory, eq) if role == "oic" else UserFactory(user_type=UserType.ADMIN)

    resp = _patch(egs_factory, editor, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]})

    assert resp.status_code == 400, resp.data
    assert resp.data["error"] == SAMPLE_SETS_DISABLED_MESSAGE
    booking.refresh_from_db()
    assert SAMPLE_SETS_KEY not in booking.input_values


@pytest.mark.django_db
def test_template_cannot_hold_extra_sets_when_off(egs_factory):
    eq = _switched_off(egs_factory)
    client = egs_factory.client_for(egs_factory.student())
    url = "/api/booking-templates/"

    bad = client.post(
        url,
        {"equipment": eq.pk, "name": "Two sets", "input_values": {"B": "1", SAMPLE_SETS_KEY: [{"B": "1"}]}},
        format="json",
    )
    assert bad.status_code == 400
    assert bad.data["error"] == SAMPLE_SETS_DISABLED_MESSAGE

    ok = client.post(url, {"equipment": eq.pk, "name": "One set", "input_values": {"B": "1"}}, format="json")
    assert ok.status_code == 201, ok.data
    edit = client.patch(
        f"{url}{ok.data['id']}/", {"input_values": {"B": "1", SAMPLE_SETS_KEY: [{"B": "1"}]}}, format="json"
    )
    assert edit.status_code == 400
    assert edit.data["error"] == SAMPLE_SETS_DISABLED_MESSAGE


# --- switched off: existing sets keep working -------------------------------------------------------


@pytest.mark.django_db
def test_existing_multi_set_booking_still_displays_and_charges_when_off(egs_factory):
    eq = _equipment(egs_factory, enable_charge_recalculation=True)
    owner = egs_factory.student()
    two_sets = {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]}
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values=two_sets)
    Equipment.objects.filter(pk=eq.pk).update(allow_multiple_sample_sets=False)

    listed = egs_factory.client_for(owner).get("/api/bookings/", {"booking_id": booking.pk, "limit": 1})
    assert listed.status_code == 200, listed.data
    assert listed.data["bookings"][0]["input_values"][SAMPLE_SETS_KEY] == [{"A": 1, "B": 1}]

    # Both sets are still timed and charged (30 min each at 10.00 / hour).
    eq.refresh_from_db()
    minutes = TimeCalculationEngine.calculate_time(booking.charge_profile, two_sets, slot_duration_minutes=60)
    charge, _breakdown = ChargeCalculationEngine.calculate_charge(booking.charge_profile, two_sets, minutes)
    assert (minutes, charge) == (60, Decimal("10.00"))

    # Values inside the existing set may still change.
    edited = _patch(egs_factory, owner, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 2, "B": 1}]})
    assert edited.status_code == 200, edited.data
    booking.refresh_from_db()
    assert booking.input_values[SAMPLE_SETS_KEY] == [{"A": 2, "B": 1}]
    assert booking.total_charge == Decimal("10.00")

    # No set may be added, even by the OIC; removing one is still allowed.
    oic = _oic(egs_factory, eq)
    added = _patch(egs_factory, oic, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 2, "B": 1}, {"A": 1}]})
    assert added.status_code == 400
    assert added.data["error"] == SAMPLE_SETS_DISABLED_MESSAGE
    removed = _patch(egs_factory, oic, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: []})
    assert removed.status_code == 200, removed.data
    booking.refresh_from_db()
    assert SAMPLE_SETS_KEY not in booking.input_values


@pytest.mark.django_db
def test_existing_multi_set_template_may_be_kept_when_off(egs_factory):
    eq = _equipment(egs_factory)
    user = egs_factory.student()
    template = BookingInputTemplate.objects.create(
        user=user, equipment=eq, name="Saved", input_values={"B": "1", SAMPLE_SETS_KEY: [{"B": "1"}]}
    )
    Equipment.objects.filter(pk=eq.pk).update(allow_multiple_sample_sets=False)
    client = egs_factory.client_for(user)

    renamed = client.patch(
        f"/api/booking-templates/{template.pk}/",
        {"name": "Saved 2", "input_values": {"B": "1", SAMPLE_SETS_KEY: [{"B": "1"}]}},
        format="json",
    )
    assert renamed.status_code == 200, renamed.data
    assert renamed.data["sample_set_count"] == 2


@pytest.mark.django_db
def test_booking_assistant_form_hides_sets_when_off(egs_factory):
    from iic_booking.research_copilot.services.assistant.booking_flow import form_reply

    user = egs_factory.student()
    for eq, expected in ((_equipment(egs_factory), True), (_switched_off(egs_factory), False)):
        reply = form_reply(user, eq)
        card = next(c for c in reply["cards"] if c.get("type") == "ba_booking_form")
        assert card["sample_sets"]["allowed"] is expected
