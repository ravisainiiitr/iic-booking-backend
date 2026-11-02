"""Automatic FBR (2 Nov 2026): the booking APIs show the portal's own I-STEM record, a manual FBR number is refused,
and an FBR executed by the portal marks the booking's FBR as verified. Fake transport, no network."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import IstemFbrStatus
from iic_booking.istem import service
from iic_booking.istem.booking_status import automatic_status
from iic_booking.istem.choices import SyncState
from iic_booking.istem.client import clear_token_cache
from iic_booking.istem.models import IstemBookingLink
from iic_booking.istem.tasks import run_istem_sync

from .support import apply_live, ready_booking

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def automatic(settings):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    return settings


def test_status_is_hidden_unless_automatic_fbr_is_on(settings):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=False)
    assert automatic_status(ready_booking()) is None


def test_status_follows_the_link(automatic):
    booking = ready_booking()
    status = automatic_status(booking)
    assert (status["step"], status["fbr_no"]) == ("queued", "")
    IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CONFIRMED, request_id="95568",
                                    fbr_no="1976095568", confirmation_id="71990")
    booking.refresh_from_db()
    status = automatic_status(booking)
    assert status["step"] == "confirmed" and status["label"] == "Slot confirmed on I-STEM"
    assert (status["fbr_no"], status["confirmation_id"]) == ("1976095568", "71990")


def test_a_link_needing_review_is_shown_as_with_the_iic_team(automatic):
    booking = ready_booking()
    IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CREATE_UNCERTAIN, needs_review=True)
    booking.refresh_from_db()
    assert automatic_status(booking)["step"] == "attention"


def test_manual_fbr_number_is_refused_in_automatic_mode(automatic):
    booking = ready_booking()
    booking.istem_fbr_status = IstemFbrStatus.PENDING_FBR
    booking.save(update_fields=["istem_fbr_status"])
    client = APIClient()
    client.force_authenticate(booking.user)
    booking.payment_settled_at = booking.created_at
    booking.save(update_fields=["payment_settled_at"])
    r = client.patch(f"/api/bookings/{booking.pk}/istem-fbr/", {"istem_fbr_number": "123"}, format="json")
    assert r.status_code == 400 and "registered on I-STEM by the portal" in r.json()["error"], r.content
    booking.refresh_from_db()
    assert booking.istem_fbr_number in ("", None) and booking.istem_fbr_status == IstemFbrStatus.PENDING_FBR


def test_executed_fbr_is_recorded_on_the_booking(automatic):
    booking = ready_booking()
    link = IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.EXECUTED, request_id="95568",
                                           fbr_no="1976095568", confirmation_id="71990", payment_id="49803")
    service._record_executed_fbr_on_booking(link)
    booking.refresh_from_db()
    assert (booking.istem_fbr_number, booking.istem_fbr_status) == ("1976095568", IstemFbrStatus.EXECUTED)
    assert booking.istem_fbr_executed_at is not None


def test_scheduled_sync_does_nothing_unless_automatic_fbr_is_on(settings):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=False)
    assert run_istem_sync() == {"skipped": True}
