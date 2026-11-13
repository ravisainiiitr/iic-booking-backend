"""Late cancellations closed on I-STEM for the fee kept, and Reschedule Booking ([TEAM 2026-10-30]), with a fake
transport (no network)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from iic_booking.equipment.models import BookingStatus
from iic_booking.istem import service
from iic_booking.istem.choices import AttemptOutcome, ErrorCategory, EvidenceType, JobState, Operation, SyncState
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.eligibility import evaluate_execute
from iic_booking.istem.errors import IstemError
from iic_booking.istem.mapping import build_execute_payload, ist_today
from iic_booking.istem.models import IstemApiAttempt, IstemOutboxJob

from .support import FakeTransport, apply_live, json_response, token_response
from .test_confirm import _created
from .test_execute import EXECUTED_OK, _admin, _confirmed

pytestmark = pytest.mark.django_db

EVIDENCE = {"evidence_type": EvidenceType.RECONCILIATION, "evidence_reference": "cancellation 2026-10-30",
            "reason": "cancelled 3 days before the slot; 75% kept under the external policy"}
UPDATED = {"status": "SUCCESS", "message": "Updated"}


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def live(settings):
    apply_live(settings, ISTEM_EXECUTE_ENABLED=True, ISTEM_CONFIRM_ENABLED=True, ISTEM_CONFIRM_APPROVED_BY="OP_IITR")
    return settings


def factory(transport):
    return lambda: IstemClient(transport)


def _cancelled(**kwargs):
    link = _confirmed(completed=False, **kwargs)
    link.booking.status = BookingStatus.CANCELLED
    link.booking.save(update_fields=["status"])
    return link


def _record(link, amount="1125.00", on=None, actor=None):
    return service.record_retained_cancellation(link, retained_amount=amount, cancelled_on=on or ist_today(),
                                                actor=actor or _admin("request_execute"), **EVIDENCE)


# --- late cancellation: FBR Execute for the retained amount --------------------------------------------------------

def test_retained_cancellation_executes_the_fbr_for_the_amount_kept(live):
    link = _cancelled()
    job = _record(link)
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_READY and job.operation == Operation.EXECUTE_FBR
    transport = FakeTransport(token_response(), json_response(EXECUTED_OK))
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.DONE
    sent = transport.calls[-1]["data"]
    assert sent["final_amount"] == "1125.00"
    assert sent["completion_date"] == ist_today().isoformat()
    assert sent["allocation_date"] == "2026-10-20"  # the slot date, before the cancellation
    assert "silent" not in sent
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTED and link.payment_id == "9981"


def test_a_slot_still_ahead_is_reported_as_allocated_on_the_cancellation_date(live):
    link = _cancelled()
    future = link.booking.daily_slots.get()
    start = future.start_datetime.replace(year=ist_today().year + 1)
    link.booking.daily_slots.update(start_datetime=start, end_datetime=start + timedelta(hours=3), date=start.date())
    link.retained_amount, link.retained_on = Decimal("750.00"), ist_today()
    payload = build_execute_payload(link.booking, link)
    assert payload.ok, payload.errors
    assert payload.payload["allocation_date"] == payload.payload["completion_date"] == ist_today().isoformat()


def test_retention_makes_a_cancelled_booking_executable_and_nothing_else(live):
    link = _cancelled()
    assert evaluate_execute(link.booking, link)[0] is False
    link.retained_amount, link.retained_on = Decimal("10.00"), ist_today()
    assert evaluate_execute(link.booking, link) == (True, "")
    link.booking.status = BookingStatus.BOOKED
    assert evaluate_execute(link.booking, link)[0] is False
    assert "not cancelled" in build_execute_payload(link.booking, link).errors["final_amount"]


@pytest.mark.parametrize("amount,on,match", [
    ("0", None, "positive"),
    ("-5", None, "positive"),
    ("10.005", None, "two decimals"),
    ("abc", None, "not a number"),
    ("1500.51", None, "more than the booking's charge"),
    ("100", date(2999, 1, 1), "future"),
])
def test_retention_amount_and_date_are_checked(live, amount, on, match):
    with pytest.raises(ValueError, match=match):
        _record(_cancelled(), amount=amount, on=on)


def test_retention_needs_a_cancelled_booking(live):
    with pytest.raises(ValueError, match="not cancelled"):
        _record(_confirmed(completed=False))


def test_retention_needs_a_confirmed_istem_booking(live):
    with pytest.raises(ValueError, match="confirmed I-STEM booking"):
        _record(_cancelled(state=SyncState.CREATED, confirmation_id=None))


def test_retention_needs_permission(live):
    with pytest.raises(PermissionError):
        _record(_cancelled(), actor=_admin())


def test_retention_needs_evidence(live):
    with pytest.raises(ValueError, match="evidence"):
        service.record_retained_cancellation(_cancelled(), retained_amount="10", cancelled_on=ist_today(),
                                            actor=_admin("request_execute"), evidence_type="", evidence_reference="", reason="")


def test_retention_needs_execute_enabled(live):
    live.ISTEM_EXECUTE_ENABLED = False
    with pytest.raises(IstemError):
        _record(_cancelled())


def test_retained_cancellation_command(live, capsys):
    link = _cancelled()
    actor = _admin("request_execute")
    call_command("istem_recover", "record-retained-cancellation", "--actor", actor.email, "--booking", str(link.booking_id),
                 "--retained-amount", "375.00", "--cancelled-on", ist_today().isoformat(),
                 "--evidence-type", EVIDENCE["evidence_type"], "--evidence-reference", EVIDENCE["evidence_reference"],
                 "--reason", EVIDENCE["reason"])
    assert "queued as job" in capsys.readouterr().out
    link.refresh_from_db()
    assert (link.retained_amount, link.sync_state) == (Decimal("375.00"), SyncState.EXECUTE_READY)
    with pytest.raises(CommandError, match="YYYY-MM-DD"):
        call_command("istem_recover", "record-retained-cancellation", "--actor", actor.email, "--booking",
                     str(link.booking_id), "--retained-amount", "1", "--cancelled-on", "30/10/2026")


# --- Reschedule Booking ---------------------------------------------------------------------------------------------

def _moved(link, days=1):
    slot = link.booking.daily_slots.get()
    link.booking.daily_slots.update(start_datetime=slot.start_datetime + timedelta(days=days),
                                    end_datetime=slot.end_datetime + timedelta(days=days))
    return link


def test_client_sends_the_reschedule_fields_with_a_placeholder_count(live):
    transport = FakeTransport(token_response(), json_response(UPDATED))
    message = IstemClient(transport).reschedule_booking("95601", timings_from="2026-11-02 10:00:00",
                                                        timings_to="2026-11-02 12:00:00", rescheduled_by="OP_IITR")
    assert message == "Updated"
    assert transport.paths()[-1] == "api/reschedule-booking"
    assert transport.calls[-1]["data"] == {
        "request_id": "95601", "rescheduled_date": "2026-11-02", "rescheduled_from_time": "2026-11-02 10:00:00",
        "rescheduled_to_time": "2026-11-02 12:00:00", "reschedule_by": "OP_IITR", "reschedule_count": "1",
        "silent": "true"}


def test_client_refuses_a_non_numeric_request_id_and_reports_istem_refusals(live):
    with pytest.raises(IstemError):
        IstemClient(FakeTransport()).reschedule_booking("FBR-1", timings_from="x", timings_to="y", rescheduled_by="OP")
    refused = FakeTransport(token_response(), json_response({"status": "ERROR", "message": "Invalid request id"}))
    with pytest.raises(IstemError) as exc:
        IstemClient(refused).reschedule_booking("1", timings_from="2026-11-02 10:00:00", timings_to="2026-11-02 11:00:00",
                                                rescheduled_by="OP")
    assert exc.value.category == ErrorCategory.BUSINESS_ERROR


def test_reschedule_moves_the_istem_booking_and_the_baseline(live):
    link = _moved(_created())
    old_from = link.create_payload["preferred_timings_from"]
    transport = FakeTransport(json_response(UPDATED))
    assert service.reschedule_on_istem(link, actor=_admin("reconcile"), reason="user moved the slot",
                                       client_factory=factory(transport)) == "Updated"
    sent = transport.calls[-1]["data"]
    link.refresh_from_db()
    assert sent["rescheduled_from_time"] == link.create_payload["preferred_timings_from"] != old_from
    assert sent["rescheduled_date"] == sent["rescheduled_from_time"][:10]
    assert not link.local_drift and link.sync_state == SyncState.CREATED
    attempt = IstemApiAttempt.objects.get(booking_link=link, operation=Operation.RESCHEDULE_BOOKING)
    assert attempt.outcome == AttemptOutcome.SUCCESS
    # The confirmation now goes out for the new window instead of being held as drift.
    job = service.request_confirmation(link, actor=_admin("request_confirm"), reason="slot allocated")
    assert IstemOutboxJob.objects.get(pk=job.pk).state == JobState.PENDING


def test_a_refused_reschedule_keeps_the_old_baseline(live):
    link = _moved(_created())
    before = dict(link.create_payload)
    refused = FakeTransport(json_response({"status": "ERROR", "message": "From Date and Time Should be less than To Date"}))
    with pytest.raises(IstemError):
        service.reschedule_on_istem(link, actor=_admin("reconcile"), reason="moved", client_factory=factory(refused))
    link.refresh_from_db()
    assert link.create_payload == before
    assert IstemApiAttempt.objects.get(booking_link=link, operation=Operation.RESCHEDULE_BOOKING).outcome == AttemptOutcome.DEFINITE_FAILURE


def test_a_server_error_on_reschedule_is_flagged_as_possibly_applied(live):
    # [STAGING 2026-10-30] HTTP 500 came back although the new timings had been applied.
    link = _moved(_created())
    before = dict(link.create_payload)
    with pytest.raises(IstemError):
        service.reschedule_on_istem(link, actor=_admin("reconcile"), reason="moved",
                                    client_factory=factory(FakeTransport(json_response({}, status=500))))
    link.refresh_from_db()
    assert link.needs_review and "may have been applied" in link.review_reason
    assert link.create_payload == before
    assert IstemApiAttempt.objects.get(booking_link=link, operation=Operation.RESCHEDULE_BOOKING).outcome == AttemptOutcome.UNCERTAIN


def test_reschedule_needs_moved_hours_and_permission(live):
    link = _created()
    with pytest.raises(ValueError, match="nothing to reschedule"):
        service.reschedule_on_istem(link, actor=_admin("reconcile"), reason="r", client_factory=factory(FakeTransport()))
    with pytest.raises(PermissionError):
        service.reschedule_on_istem(_moved(link), actor=_admin(), reason="r", client_factory=factory(FakeTransport()))


def test_reschedule_needs_a_created_unconfirmed_link(live):
    confirmed = _confirmed()
    with pytest.raises(ValueError, match="created, unconfirmed"):
        service.reschedule_on_istem(confirmed, actor=_admin("reconcile"), reason="r", client_factory=factory(FakeTransport()))
    actor = _admin("reconcile")
    with pytest.raises(CommandError, match="created, unconfirmed"):
        call_command("istem_recover", "reschedule", "--actor", actor.email, "--booking", str(confirmed.booking_id), "--reason", "r")
