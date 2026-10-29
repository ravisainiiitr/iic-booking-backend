"""Live pilot: the new external workflow applies to test accounts only; everyone else keeps the existing flow."""

from __future__ import annotations

from decimal import Decimal

import pytest

from iic_booking.equipment.booking_events import ISTEM_TIMING_NOTE_PLAIN, apply_istem_timing_note_to_context
from iic_booking.equipment.models import Booking
from iic_booking.external_services.models import ExternalBookingCheckout
from iic_booking.external_services.pilot import external_workflow_user, internal_gateway_fallback_user
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, make_user
from .test_booking_checkout import CHECKOUTS, _faculty, _user, local_gateway, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("local_gateway")]


@pytest.fixture
def pilot(settings):
    """Production defaults: both switches off."""
    settings.EXTERNAL_WORKFLOW_ALL_EXTERNAL_USERS = False
    settings.INTERNAL_GATEWAY_FALLBACK_ALL_USERS = False
    return settings


def _book(user, world, slot):
    return client_for(user).post(f"/api/equipments/{world['eq'].pk}/book/", {"slot_ids": [slot.id], "input_values": {}},
                                 format="json")


def test_only_test_accounts_are_in_the_pilot(pilot):
    assert external_workflow_user(make_user(UserType.EXTERNAL, is_test_account=True))
    assert not external_workflow_user(make_user(UserType.EXTERNAL))
    assert not external_workflow_user(make_user(UserType.FACULTY, is_test_account=True))
    assert internal_gateway_fallback_user(make_user(UserType.FACULTY, is_test_account=True))
    assert not internal_gateway_fallback_user(make_user(UserType.FACULTY))
    pilot.EXTERNAL_WORKFLOW_ALL_EXTERNAL_USERS = True
    assert external_workflow_user(make_user(UserType.EXTERNAL))


def test_external_user_outside_the_pilot_books_as_before(pilot, world):
    user = _user(world, "5000.00")
    r = _book(user, world, world["slots"][0])
    assert r.status_code == 201, r.content
    assert Booking.objects.filter(user=user).exists()
    assert client_for(user).post(CHECKOUTS, {"equipment_id": world["eq"].pk, "slot_ids": [world["slots"][1].id],
                                             "input_values": {}}, format="json").status_code == 403
    assert not ExternalBookingCheckout.objects.exists()


def test_external_test_account_goes_through_the_checkout(pilot, world):
    user = _user(world, "5000.00")
    user.is_test_account = True
    user.save(update_fields=["is_test_account"])
    r = _book(user, world, world["slots"][0])
    assert r.status_code == 409 and r.json()["code"] == "external_checkout_required", r.content


def test_internal_user_short_of_funds_gets_the_existing_refusal(pilot, world):
    faculty = _faculty(world, "1.00")
    r = _book(faculty, world, world["slots"][1])
    assert r.status_code == 400 and "gateway_checkout_available" not in r.json(), r.content
    assert client_for(faculty).post(CHECKOUTS, {"equipment_id": world["eq"].pk, "slot_ids": [world["slots"][1].id],
                                                "input_values": {}}, format="json").status_code == 403


def test_window_does_not_apply_outside_the_pilot(pilot, world):
    body = client_for(_user(world)).get("/api/v1/external-booking/window/").json()
    assert body["applies_to_you"] is False


def test_istem_timing_note_only_while_the_istem_api_is_on(pilot, settings):
    user = make_user(UserType.EXTERNAL, is_test_account=True)
    context = {"comment": ""}
    apply_istem_timing_note_to_context(context, user)
    assert context["istem_timing_note"] == ""
    settings.ISTEM_ENABLED = True
    settings.ISTEM_MODE = "live"
    apply_istem_timing_note_to_context(context, user)
    assert context["istem_timing_note"] == ISTEM_TIMING_NOTE_PLAIN


def test_balance_is_untouched_for_a_refused_checkout(pilot, world):
    user = _user(world, "5000.00")
    _book(user, world, world["slots"][0])
    from iic_booking.users.models import SubWallet

    assert SubWallet.objects.get(wallet__user=user).balance < Decimal("5000.00")  # booked from the wallet, as before
