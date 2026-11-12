"""The booking page says why a booking that needs an I-STEM FBR has not been registered yet (8 Nov 2026)."""

from __future__ import annotations

import pytest

from iic_booking.istem.booking_status import automatic_status

from .support import apply_live, make_booking, map_codes, map_equipment, ready_booking

pytestmark = pytest.mark.django_db


@pytest.fixture
def automatic(settings):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    return settings


def _reasons(booking):
    return automatic_status(booking, include_reason=True)["waiting_reasons"]


def test_a_ready_booking_is_simply_being_registered(automatic):
    assert _reasons(ready_booking()) == ["Being registered on I-STEM; this usually takes a few minutes."]


def test_an_unlinked_account_is_named_first(automatic):
    booking = make_booking()
    map_equipment(booking.equipment)
    map_codes(booking.equipment)
    reasons = _reasons(booking)
    assert reasons[0].startswith("Your I-STEM account is not linked yet. The IIC team links it")


def test_with_automatic_linking_the_user_is_told_it_happens_by_itself(automatic):
    automatic.ISTEM_USER_CREATION_ENABLED = True
    booking = make_booking()
    map_equipment(booking.equipment)
    map_codes(booking.equipment)
    booking.user.gender, booking.user.phone_number = "female", "9876543210"
    booking.user.save(update_fields=["gender", "phone_number"])
    assert _reasons(booking)[0].startswith("Your I-STEM account is being set up automatically")
    booking.user.gender = ""
    booking.user.save(update_fields=["gender"])
    assert _reasons(booking)[0] == "Your I-STEM account is created automatically once your profile has: Select your gender."


def test_missing_billing_and_an_unmapped_instrument_are_explained(automatic):
    booking = make_booking(with_billing=False)
    status = automatic_status(booking, include_reason=True)
    text = " ".join(status["waiting_reasons"])
    assert "billing details are incomplete" in text and "not on I-STEM" in text
    assert any(line.startswith("equipment_id:") for line in status["waiting_detail"])


def test_lists_do_not_compute_the_reason(automatic):
    status = automatic_status(ready_booking())
    assert "waiting_reasons" not in status


def test_no_reason_once_registered(automatic):
    from iic_booking.istem.choices import SyncState
    from iic_booking.istem.models import IstemBookingLink

    booking = ready_booking()
    IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CONFIRMED, request_id="1", fbr_no="2", confirmation_id="3")
    booking.refresh_from_db()
    assert "waiting_reasons" not in automatic_status(booking, include_reason=True)
