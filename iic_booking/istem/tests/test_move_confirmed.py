"""A slot moved on the portal after I-STEM confirmed the booking (I-STEM, 13 Nov 2026): Reschedule is refused for a
confirmed booking, so a new booking is created and confirmed and the old one cancelled; overlapping slots cancel the
old one first. Fake transport (no network)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from iic_booking.equipment.models import BookingStatus
from iic_booking.istem import service
from iic_booking.istem.choices import JobState, Operation, SupersededState, SyncState
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.models import IstemOutboxJob, IstemSupersededBooking
from iic_booking.istem.outbox import enqueue_booking_sync

from .support import FakeTransport, apply_live, json_response, ready_booking, token_response

pytestmark = pytest.mark.django_db

CREATED_OLD = {"status": "Sucess", "message": "Done", "new_service_id": 55401, "fbr_no": "1976125722"}
CREATED_NEW = {"status": "Sucess", "message": "Done", "new_service_id": 55977, "fbr_no": "1976125999"}
CONFIRMED_OLD = {"status": "SUCCESS", "message": "Booking confirmed", "result": {"request_id": "55401", "confirmation_id": "44294"}}
CONFIRMED_NEW = {"status": "SUCCESS", "message": "Booking confirmed", "result": {"request_id": "55977", "confirmation_id": "44300"}}
CANCELLED_OK = {"status": "Sucess", "message": "Done"}


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def live(settings):
    apply_live(settings, ISTEM_CONFIRM_ENABLED=True, ISTEM_AUTO_CONFIRM=True, ISTEM_CANCEL_ENABLED=True,
               ISTEM_CONFIRM_APPROVED_BY="IIT Roorkee Booking Portal", ISTEM_SILENT_MODE_FIELD="silent",
               ISTEM_SILENT_MODE_VALUE="true")
    return settings


def factory(transport):
    return lambda: IstemClient(transport)


def _run(link, operation, *bodies):
    job = IstemOutboxJob.objects.get(booking_link=link, operation=operation, state=JobState.PENDING)
    clear_token_cache()  # every fake exchange starts with the token request
    transport = FakeTransport(token_response(), *(json_response(b) for b in bodies))
    state = service.process_job(job.pk, client_factory=factory(transport))
    link.refresh_from_db()
    return state, transport


def _confirmed_through_the_real_path():
    link = enqueue_booking_sync(ready_booking(), trigger="test")
    _run(link, Operation.CREATE_BOOKING, CREATED_OLD)
    assert link.sync_state == SyncState.CONFIRM_READY
    _run(link, Operation.CONFIRM_BOOKING, CONFIRMED_OLD)
    assert link.sync_state == SyncState.CONFIRMED and link.confirmation_id == "44294"
    return link


def _move(link, delta):
    for slot in link.booking.daily_slots.all():
        slot.start_datetime += delta
        slot.end_datetime += delta
        slot.date = slot.start_datetime.date()
        slot.save(update_fields=["start_datetime", "end_datetime", "date"])


def test_an_unchanged_confirmed_booking_is_left_alone(live):
    _confirmed_through_the_real_path()
    assert service.enqueue_moves() == 0


def test_a_move_to_a_separate_slot_creates_and_confirms_first_then_cancels_the_old_booking(live):
    link = _confirmed_through_the_real_path()
    _move(link, timedelta(days=1))

    assert service.enqueue_moves() == 1
    link.refresh_from_db()
    row = IstemSupersededBooking.objects.get(booking_link=link)
    assert link.sync_state == SyncState.CREATE_READY and link.request_id is None and link.confirmation_id is None
    assert (row.request_id, row.fbr_no, row.confirmation_id, row.cancel_first) == ("55401", "1976125722", "44294", False)
    assert service.enqueue_superseded_cancellations() == 0  # the old one stays until the new one is confirmed

    _, sent = _run(link, Operation.CREATE_BOOKING, CREATED_NEW)
    assert link.request_id == "55977" and sent.calls[-1]["data"]["silent"] == "true"
    _run(link, Operation.CONFIRM_BOOKING, CONFIRMED_NEW)
    assert link.sync_state == SyncState.CONFIRMED and link.confirmation_id == "44300"

    assert service.enqueue_superseded_cancellations() == 1
    state, sent = _run(link, Operation.CANCEL_SUPERSEDED, CANCELLED_OK)
    assert state == JobState.DONE
    assert sent.paths()[-1] == "api/cancel-booking"
    assert sent.calls[-1]["data"]["request_id"] == "55401" and sent.calls[-1]["data"]["silent"] == "true"
    row.refresh_from_db()
    assert row.state == SupersededState.CANCELLED and row.cancelled_at is not None
    assert link.sync_state == SyncState.CONFIRMED and link.request_id == "55977" and not link.needs_review
    assert service.enqueue_moves() == 0  # the new booking matches the portal slot


def test_an_overlapping_move_cancels_the_old_booking_before_creating_the_new_one(live):
    link = _confirmed_through_the_real_path()
    _move(link, timedelta(hours=1))

    assert service.enqueue_moves() == 1
    row = IstemSupersededBooking.objects.get(booking_link=link)
    assert row.cancel_first
    assert not IstemOutboxJob.objects.filter(booking_link=link, operation=Operation.CREATE_BOOKING, state=JobState.PENDING).exists()

    _, sent = _run(link, Operation.CANCEL_SUPERSEDED, CANCELLED_OK)
    assert sent.calls[-1]["data"]["request_id"] == "55401"
    row.refresh_from_db()
    assert row.state == SupersededState.CANCELLED
    _run(link, Operation.CREATE_BOOKING, CREATED_NEW)
    _run(link, Operation.CONFIRM_BOOKING, CONFIRMED_NEW)
    assert link.sync_state == SyncState.CONFIRMED and link.request_id == "55977"


def test_a_refused_cancel_of_the_old_booking_is_held_for_review(live):
    link = _confirmed_through_the_real_path()
    _move(link, timedelta(hours=1))
    service.enqueue_moves()

    state, _ = _run(link, Operation.CANCEL_SUPERSEDED, {"status": False, "message": "Booking not found"})
    assert state == JobState.BLOCKED
    row = IstemSupersededBooking.objects.get(booking_link=link)
    assert row.state == SupersededState.CANCEL_FAILED
    assert link.needs_review and "55401" in link.review_reason
    assert not IstemOutboxJob.objects.filter(booking_link=link, operation=Operation.CREATE_BOOKING, state=JobState.PENDING).exists()


def test_a_slot_moved_into_the_past_or_a_cancelled_booking_is_not_moved(live):
    link = _confirmed_through_the_real_path()
    _move(link, timedelta(days=-5))
    assert service.enqueue_moves() == 0

    link.booking.status = BookingStatus.REFUNDED
    link.booking.save(update_fields=["status"])
    _move(link, timedelta(days=8))
    assert service.enqueue_moves() == 0


def test_moves_can_be_switched_off(live, settings):
    link = _confirmed_through_the_real_path()
    _move(link, timedelta(days=1))
    settings.ISTEM_MOVE_CONFIRMED_ENABLED = False
    assert service.enqueue_moves() == 0


def test_reschedule_is_sent_silently():
    from iic_booking.istem import contract

    assert "reschedule_booking" in contract.SILENT_OPERATIONS
