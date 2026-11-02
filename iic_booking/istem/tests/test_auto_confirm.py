"""Confirm Booking follows Create Booking automatically (2 Nov 2026): the user picks an open slot and pays, so nobody
confirms it by hand. Fake transport, no network."""

from __future__ import annotations

import pytest

from iic_booking.istem import contract, service
from iic_booking.istem.choices import JobState, Operation, SyncState
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.models import IstemOutboxJob
from iic_booking.istem.outbox import enqueue_booking_sync

from .support import FakeTransport, apply_live, json_response, ready_booking, token_response

pytestmark = pytest.mark.django_db

CREATED = {"status": "Sucess", "message": "Done", "file_path": "x.pdf", "new_service_id": 55401, "fbr_no": "1976125722"}
CONFIRMED = {"status": "SUCCESS", "message": "Booking confirmed", "result": {"request_id": "55401", "confirmation_id": "44294"}}


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


def _factory(transport):
    return lambda: IstemClient(transport)


def _create(booking):
    link = enqueue_booking_sync(booking, trigger="test")
    job = IstemOutboxJob.objects.get(booking_link=link, operation=Operation.CREATE_BOOKING, state=JobState.PENDING)
    assert service.process_job(job.pk, client_factory=_factory(FakeTransport(token_response(), json_response(CREATED)))) == JobState.DONE
    link.refresh_from_db()
    return link


def test_confirm_is_queued_and_sent_after_create(settings):
    apply_live(settings, ISTEM_CONFIRM_ENABLED=True, ISTEM_CONFIRM_APPROVED_BY="OP_IITR", ISTEM_AUTO_CONFIRM=True)
    link = _create(ready_booking())
    assert (link.sync_state, link.request_id) == (SyncState.CONFIRM_READY, "55401")
    job = IstemOutboxJob.objects.get(booking_link=link, operation=Operation.CONFIRM_BOOKING)
    assert (job.state, job.trigger) == (JobState.PENDING, "automatic")

    transport = FakeTransport(json_response(CONFIRMED))
    assert service.process_job(job.pk, client_factory=_factory(transport)) == JobState.DONE
    link.refresh_from_db()
    assert (link.sync_state, link.confirmation_id) == (SyncState.CONFIRMED, "44294")
    assert transport.paths() == [contract.CONFIRM_BOOKING_PATH]


def test_nothing_is_confirmed_while_confirmation_is_off(settings):
    apply_live(settings, ISTEM_CONFIRM_ENABLED=False, ISTEM_AUTO_CONFIRM=True)
    link = _create(ready_booking())
    assert link.sync_state == SyncState.CREATED
    assert not IstemOutboxJob.objects.filter(booking_link=link, operation=Operation.CONFIRM_BOOKING).exists()


def test_automatic_confirmation_can_be_turned_off(settings):
    apply_live(settings, ISTEM_CONFIRM_ENABLED=True, ISTEM_CONFIRM_APPROVED_BY="OP_IITR", ISTEM_AUTO_CONFIRM=False)
    link = _create(ready_booking())
    assert link.sync_state == SyncState.CREATED
    assert not IstemOutboxJob.objects.filter(booking_link=link, operation=Operation.CONFIRM_BOOKING).exists()
