"""Portal cancellations closed on I-STEM ([TEAM 2026-10-30]): a full refund by Cancel Booking, a cancellation that kept
the fee by FBR Execute for the amount kept. Fake transport (no network)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from iic_booking.equipment.models import BookingStatus
from iic_booking.istem import service
from iic_booking.istem.choices import ErrorCategory, JobState, Operation, RemoteStatus, SyncState
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.errors import IstemError
from iic_booking.istem.models import IstemOutboxJob
from iic_booking.istem.tasks import run_istem_sync

from .support import FakeTransport, apply_live, json_response, token_response
from .test_confirm import _created
from .test_execute import EXECUTED_OK, _confirmed

pytestmark = pytest.mark.django_db

CANCELLED_OK = {"status": "Sucess", "message": "Done", "file_path": "cancelled Sucessfully"}


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def live(settings):
    apply_live(settings, ISTEM_EXECUTE_ENABLED=True, ISTEM_CONFIRM_ENABLED=True, ISTEM_CANCEL_ENABLED=True,
               ISTEM_CONFIRM_APPROVED_BY="IIT Roorkee Booking Portal", ISTEM_SILENT_MODE_FIELD="silent",
               ISTEM_SILENT_MODE_VALUE="true")
    return settings


def factory(transport):
    return lambda: IstemClient(transport)


def _cancel_locally(link, status):
    link.booking.status = status
    link.booking.save(update_fields=["status"])
    return link


def _job(link, operation):
    return IstemOutboxJob.objects.get(booking_link=link, operation=operation, state=JobState.PENDING)


def test_a_full_refund_is_cancelled_on_istem(live):
    link = _cancel_locally(_confirmed(completed=False), BookingStatus.REFUNDED)
    assert service.enqueue_cancellations() == 1
    link.refresh_from_db()
    assert link.sync_state == SyncState.CANCEL_READY
    transport = FakeTransport(token_response(), json_response(CANCELLED_OK))
    assert service.process_job(_job(link, Operation.CANCEL_BOOKING).pk, client_factory=factory(transport)) == JobState.DONE
    link.refresh_from_db()
    assert link.sync_state == SyncState.REMOTE_CANCELLED and link.remote_status == RemoteStatus.CANCELLED
    sent = transport.calls[-1]
    assert transport.paths()[-1] == "api/cancel-booking"
    assert sent["data"]["request_id"] == "55401" and sent["data"]["silent"] == "true"
    assert sent["data"]["cancelled_by"] == "IIT Roorkee Booking Portal"


def test_a_booking_created_but_not_confirmed_is_cancelled_too(live):
    link = _cancel_locally(_created(), BookingStatus.REFUNDED)
    assert service.enqueue_cancellations() == 1
    link.refresh_from_db()
    assert link.sync_state == SyncState.CANCEL_READY


def test_a_cancellation_that_kept_the_fee_is_executed_for_the_amount_kept(live):
    link = _cancel_locally(_confirmed(completed=False), BookingStatus.CANCELLED)
    assert service.enqueue_cancellations() == 1
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_READY and link.retained_amount == Decimal("1500.50")
    transport = FakeTransport(token_response(), json_response(EXECUTED_OK))
    assert service.process_job(_job(link, Operation.EXECUTE_FBR).pk, client_factory=factory(transport)) == JobState.DONE
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTED
    assert transport.calls[-1]["data"]["final_amount"] == "1500.50"


def test_fee_kept_before_the_slot_was_confirmed_goes_to_the_iic_team(live):
    link = _cancel_locally(_created(), BookingStatus.CANCELLED)
    assert service.enqueue_cancellations() == 0
    link.refresh_from_db()
    assert link.sync_state == SyncState.MANUAL_REVIEW and link.needs_review
    assert not IstemOutboxJob.objects.filter(booking_link=link, operation__in=[Operation.CANCEL_BOOKING, Operation.EXECUTE_FBR]).exists()


def test_nothing_happens_while_cancellation_is_off(live, settings):
    settings.ISTEM_CANCEL_ENABLED = False
    link = _cancel_locally(_confirmed(completed=False), BookingStatus.REFUNDED)
    assert service.enqueue_cancellations() == 0
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRMED


def test_active_bookings_are_left_alone(live):
    link = _confirmed(completed=False)
    assert service.enqueue_cancellations() == 0
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRMED


def test_a_refused_cancel_waits_for_the_iic_team(live):
    link = _cancel_locally(_confirmed(completed=False), BookingStatus.REFUNDED)
    service.enqueue_cancellations()
    transport = FakeTransport(token_response(), json_response({"status": False, "message": "Booking already executed"}))
    assert service.process_job(_job(link, Operation.CANCEL_BOOKING).pk, client_factory=factory(transport)) == JobState.BLOCKED
    link.refresh_from_db()
    assert link.sync_state == SyncState.CANCEL_FAILED and link.needs_review


def test_an_uncertain_cancel_is_not_repeated(live):
    link = _cancel_locally(_confirmed(completed=False), BookingStatus.REFUNDED)
    service.enqueue_cancellations()
    transport = FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "read timed out"))
    assert service.process_job(_job(link, Operation.CANCEL_BOOKING).pk, client_factory=factory(transport)) == JobState.BLOCKED
    link.refresh_from_db()
    assert link.sync_state == SyncState.CANCEL_UNCERTAIN and link.needs_review
    assert service.enqueue_cancellations() == 0


def test_a_booking_restored_before_sending_is_not_cancelled(live):
    link = _cancel_locally(_confirmed(completed=False), BookingStatus.REFUNDED)
    service.enqueue_cancellations()
    _cancel_locally(link, BookingStatus.BOOKED)
    assert service.process_job(_job(link, Operation.CANCEL_BOOKING).pk, client_factory=factory(FakeTransport())) == JobState.CANCELLED
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRMED


def test_the_scheduled_sync_closes_cancelled_bookings(live, settings, monkeypatch):
    settings.ISTEM_AUTOMATIC_FBR = True
    link = _cancel_locally(_confirmed(completed=False), BookingStatus.REFUNDED)
    transport = FakeTransport(token_response(), json_response(CANCELLED_OK))
    process = service.process_due_jobs
    monkeypatch.setattr(service, "process_due_jobs", lambda limit: process(limit, client_factory=factory(transport)))
    summary = run_istem_sync()
    assert summary["queued_cancellations"] == 1
    link.refresh_from_db()
    assert link.sync_state == SyncState.REMOTE_CANCELLED


def test_the_sync_schedule_is_installed_switched_off():
    from django_celery_beat.models import PeriodicTask

    task = PeriodicTask.objects.select_related("crontab").get(name="I-STEM sync (every 5 minutes)")
    assert task.task == "istem.run_sync" and not task.enabled and task.crontab.minute == "*/5"
