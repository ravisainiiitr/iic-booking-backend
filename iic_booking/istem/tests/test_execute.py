"""FBR Execute: payload mapping, gating, duplicate triggers, uncertain outcomes and recovery, with a fake transport
(no network)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from iic_booking.equipment.models import BookingStatus
from iic_booking.istem import contract, service
from iic_booking.istem.choices import AttemptOutcome, ErrorCategory, EvidenceType, JobState, Operation, RemoteStatus, SyncState
from iic_booking.istem.client import IstemClient, TransportResponse, clear_token_cache
from iic_booking.istem.errors import IstemError
from iic_booking.istem.mapping import booking_slot_window, build_execute_payload
from iic_booking.istem.models import IstemApiAttempt, IstemBookingLink, IstemOutboxJob, IstemSyncEvent, IstemUserLink
from iic_booking.istem.outbox import enqueue_fbr_execute
from iic_booking.users.tests.factories import UserFactory

from .support import FAKE_PASSWORD, FAKE_TOKEN, FakeTransport, apply_live, json_response, ready_booking, token_response

pytestmark = pytest.mark.django_db

# [SPEC] /api/fbr-execute success example.
EXECUTED_OK = {"status": "SUCCESS", "message": "FBR is Marked as Executed successfully", "payment_id": 9981,
               "request_id": 55401, "confirmation_id": 44294}
ALREADY_EXECUTED = {"status": "SUCCESS", "message": "FBR was already marked as Executed", "payment_id": 9981,
                    "request_id": "55401", "confirmation_id": "44294"}
COMPLETED_AT = datetime(2026, 10, 20, 4, 45, 30, tzinfo=dt_timezone.utc)
SLOT_START = datetime(2026, 10, 19, 20, 0, 0, tzinfo=dt_timezone.utc)  # 2026-10-20 01:30 IST: a different UTC date
EXECUTE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EVIDENCE = {"evidence_type": EvidenceType.RECONCILIATION, "evidence_reference": "get-fbr-details 2026-10-25",
            "reason": "I-STEM still reports Slot Allocated after the timed-out execute"}


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def live(settings):
    apply_live(settings, ISTEM_EXECUTE_ENABLED=True)
    return settings


def factory(transport):
    return lambda: IstemClient(transport)


def _confirmed(*, total=Decimal("1500.50"), completed=True, confirmation_id="44294", state=SyncState.CONFIRMED):
    """A booking created and confirmed on I-STEM, completed locally (unless ``completed`` is False)."""
    booking = ready_booking(total=total)
    # A completed booking's slot lies in the past, before completion (the fixture slot is two days ahead).
    booking.daily_slots.update(start_datetime=SLOT_START, end_datetime=SLOT_START + timedelta(hours=3), date=SLOT_START.date())
    if completed:
        booking.status = BookingStatus.COMPLETED
        booking.completed_at = COMPLETED_AT
        booking.save(update_fields=["status", "completed_at"])
    return IstemBookingLink.objects.create(
        booking=booking, user_link=IstemUserLink.objects.get(user=booking.user), request_id="55401",
        fbr_no="1976125722", confirmation_id=confirmation_id, sync_state=state, remote_status=RemoteStatus.SLOT_ALLOCATED,
    )


def _queue(link):
    enqueue_fbr_execute(link.booking, trigger="test")
    return IstemOutboxJob.objects.get(booking_link=link, operation=Operation.EXECUTE_FBR, state=JobState.PENDING)


def _admin(*perms):
    user = UserFactory(admin_approved=True, is_staff=True)
    for codename in perms:
        user.user_permissions.add(Permission.objects.get(content_type__app_label="istem", codename=codename))
    return type(user).objects.get(pk=user.pk)  # drop the permission cache


def _local_snapshot(booking):
    booking.refresh_from_db()
    return booking.status, booking.completed_at, booking.total_charge, booking.istem_fbr_status, booking.istem_fbr_number


def _rows(code):
    return json_response({"status": "SUCCESS", "result": [{"fbr_no": "1976125722", "status_code": code}]})


# --- payload ---------------------------------------------------------------------------------------------------------

def test_payload_maps_ids_dates_and_final_amount_exactly(live):
    link = _confirmed()
    booking = link.booking
    result = build_execute_payload(booking, link)
    assert result.ok, result.errors
    p = result.payload
    assert set(p) == set(contract.FBR_EXECUTE_FIELDS)  # updated_by omitted by default
    assert (p["request_id"], p["confirmation_id"]) == ("55401", "44294")
    # [SPEC] YYYY-MM-DD, IST calendar dates.
    assert EXECUTE_DATE.match(p["completion_date"]) and EXECUTE_DATE.match(p["allocation_date"])
    assert p["completion_date"] == "2026-10-20"  # 04:45 UTC = 10:15 IST
    start, _ = booking_slot_window(booking)
    assert start == SLOT_START and p["allocation_date"] == "2026-10-20"  # 20:00 UTC on the 19th = 01:30 IST on the 20th
    assert p["final_amount"] == "1500.50"  # the recorded charge, not rounded
    assert all(isinstance(v, str) for v in p.values())


def test_whole_amount_is_sent_with_two_decimals_unchanged(live):
    link = _confirmed(total=Decimal("1500"))
    assert build_execute_payload(link.booking, link).payload["final_amount"] == "1500.00"


@pytest.mark.parametrize("total", [Decimal("-1.00"), Decimal("10.005"), None])
def test_invalid_final_amount_is_blocked_not_altered(live, total):
    link = _confirmed()
    link.booking.total_charge = total
    result = build_execute_payload(link.booking, link)
    assert "final_amount" in result.errors and "final_amount" not in result.payload


def test_pending_charge_recalculation_blocks_final_amount(live):
    link = _confirmed()
    link.booking.charge_recalculation_pending_amount = Decimal("200.00")
    assert "final_amount" in build_execute_payload(link.booking, link).errors


def test_updated_by_is_sent_only_when_configured_and_valid(live):
    link = _confirmed()
    assert "updated_by" not in build_execute_payload(link.booking, link).payload
    live.ISTEM_EXECUTE_UPDATED_BY = "OP_IITR"
    assert build_execute_payload(link.booking, link).payload["updated_by"] == "OP_IITR"
    live.ISTEM_EXECUTE_UPDATED_BY = "OP;DROP"
    assert "updated_by" in build_execute_payload(link.booking, link).errors


@pytest.mark.parametrize("field", ["request_id", "confirmation_id"])
def test_missing_remote_ids_are_reported_not_invented(live, field):
    link = _confirmed()
    IstemBookingLink.objects.filter(pk=link.pk).update(**{field: None})
    link.refresh_from_db()
    result = build_execute_payload(link.booking, link)
    assert field in result.errors and field not in result.payload


def test_missing_allocation_window_is_reported(live):
    link = _confirmed()
    link.booking.daily_slots.all().delete()
    result = build_execute_payload(link.booking, link)
    assert "allocation_date" in result.errors and "allocation_date" not in result.payload


def test_incomplete_booking_has_no_completion_date(live):
    link = _confirmed(completed=False)
    assert "completion_date" in build_execute_payload(link.booking, link).errors


# --- eligibility and queueing ----------------------------------------------------------------------------------------

def test_disabled_by_default_writes_nothing_and_sends_nothing(settings):
    apply_live(settings)  # ISTEM_EXECUTE_ENABLED left at its default
    link = _confirmed()
    assert enqueue_fbr_execute(link.booking, trigger="test") is None
    assert service.enqueue_eligible_executions() == 0
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRMED
    assert not IstemOutboxJob.objects.exists()


def test_integration_off_writes_nothing_even_with_execute_enabled(settings):
    settings.ISTEM_ENABLED, settings.ISTEM_EXECUTE_ENABLED = False, True
    link = _confirmed()
    assert enqueue_fbr_execute(link.booking, trigger="test") is None
    assert service.enqueue_eligible_executions() == 0
    assert not IstemOutboxJob.objects.exists()


def test_disabled_switch_blocks_an_existing_job_without_a_request(live):
    link = _confirmed()
    job = _queue(link)
    live.ISTEM_EXECUTE_ENABLED = False
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []


def test_booking_not_completed_is_not_queued(live):
    link = _confirmed(completed=False)
    enqueue_fbr_execute(link.booking, trigger="test")
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRMED
    assert not IstemOutboxJob.objects.exists()


def test_completed_status_without_completion_time_is_not_queued(live):
    link = _confirmed()
    type(link.booking).objects.filter(pk=link.booking_id).update(completed_at=None)
    link.booking.refresh_from_db()
    enqueue_fbr_execute(link.booking, trigger="test")
    assert service.enqueue_eligible_executions() == 0
    assert not IstemOutboxJob.objects.exists()


def test_unsettled_payment_is_not_queued(live):
    link = _confirmed()
    type(link.booking).objects.filter(pk=link.booking_id).update(amount_due=Decimal("100.00"), payment_settled_at=None)
    link.booking.refresh_from_db()
    enqueue_fbr_execute(link.booking, trigger="test")
    assert not IstemOutboxJob.objects.exists()


def test_booking_never_sent_to_istem_is_ignored(live):
    booking = ready_booking()
    booking.status, booking.completed_at = BookingStatus.COMPLETED, COMPLETED_AT
    booking.save(update_fields=["status", "completed_at"])
    assert enqueue_fbr_execute(booking, trigger="test") is None
    assert not IstemBookingLink.objects.exists()


@pytest.mark.parametrize("state,confirmation_id", [
    (SyncState.CREATED, None),
    (SyncState.CONFIRMED_ID_MISSING, None),
    (SyncState.CONFIRM_UNCERTAIN, None),
    (SyncState.CONFIRMED, None),
])
def test_without_a_confirmation_id_nothing_is_queued(live, state, confirmation_id):
    link = _confirmed(state=state, confirmation_id=confirmation_id)
    enqueue_fbr_execute(link.booking, trigger="test")
    assert service.enqueue_eligible_executions() == 0
    assert not IstemOutboxJob.objects.exists()


@pytest.mark.parametrize("state", [
    SyncState.EXECUTE_IN_FLIGHT, SyncState.EXECUTE_UNCERTAIN, SyncState.EXECUTE_FAILED,
    SyncState.EXECUTED_PENDING_VERIFY, SyncState.EXECUTED,
])
def test_in_flight_uncertain_failed_or_executed_links_are_not_requeued(live, state):
    link = _confirmed(state=state)
    enqueue_fbr_execute(link.booking, trigger="complete_booking")
    assert service.enqueue_eligible_executions() == 0
    link.refresh_from_db()
    assert link.sync_state == state
    assert not IstemOutboxJob.objects.exists()


def test_repeated_completion_signals_keep_one_job(live):
    link = _confirmed()
    for trigger in ("complete_booking", "auto_complete", "discovery"):
        enqueue_fbr_execute(link.booking, trigger=trigger)
    assert service.enqueue_eligible_executions() == 1  # the link is already EXECUTE_READY; its job is reused
    assert IstemOutboxJob.objects.filter(operation=Operation.EXECUTE_FBR).count() == 1
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_READY
    assert IstemSyncEvent.objects.filter(booking_link=link, to_state=SyncState.EXECUTE_READY).count() == 1


def test_sync_command_queues_completed_confirmed_bookings(live):
    link = _confirmed()
    call_command("istem_sync", "--enqueue")
    assert IstemOutboxJob.objects.filter(booking_link=link, operation=Operation.EXECUTE_FBR, state=JobState.PENDING).exists()


def test_shadow_mode_validates_without_a_request(settings):
    settings.ISTEM_ENABLED, settings.ISTEM_MODE, settings.ISTEM_EXECUTE_ENABLED = True, "shadow", True
    link = _confirmed()
    job = _queue(link)
    assert service.process_job(job.pk, client_factory=lambda: pytest.fail("no client in shadow mode")) == JobState.DONE
    assert not IstemApiAttempt.objects.exists()


def test_booking_reopened_before_processing_is_cancelled_without_a_request(live):
    link = _confirmed()
    job = _queue(link)
    booking = link.booking
    booking.status = BookingStatus.PROCESSING
    booking.save(update_fields=["status"])
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.CANCELLED
    assert transport.calls == []
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRMED


def test_payload_problem_at_processing_time_blocks_without_a_request(live):
    link = _confirmed()
    job = _queue(link)
    link.booking.daily_slots.all().delete()
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []
    link.refresh_from_db()
    assert link.needs_review and "allocation_date" in link.blocked_fields


# --- remote outcomes -------------------------------------------------------------------------------------------------

def test_success_posts_the_form_and_records_the_payment_id(live):
    link = _confirmed()
    before = _local_snapshot(link.booking)
    job = _queue(link)
    transport = FakeTransport(token_response(), json_response(EXECUTED_OK))
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.DONE
    assert transport.paths() == [contract.AUTH_TOKEN_PATH, contract.FBR_EXECUTE_PATH]
    call = transport.calls[1]
    assert call["method"] == "POST" and call["headers"][contract.TOKEN_HEADER] == FAKE_TOKEN
    assert call["headers"]["Content-Type"] == contract.FORM_CONTENT_TYPE
    assert call["data"] == build_execute_payload(link.booking, link).payload
    link.refresh_from_db()
    # The documented answer (status SUCCESS, payment_id, the same request_id and confirmation_id) is definitive.
    assert (link.sync_state, link.payment_id, link.remote_status, link.needs_review) == (
        SyncState.EXECUTED, "9981", RemoteStatus.EXECUTED, False)
    assert link.executed_at is not None
    attempt = IstemApiAttempt.objects.get(operation=Operation.EXECUTE_FBR)
    assert attempt.outcome == AttemptOutcome.SUCCESS and attempt.response_redacted["payment_id"] == "9981"
    assert _local_snapshot(link.booking) == before


def test_get_fbr_details_executed_resolves_a_provisional_link_and_flags_the_unknown_payment_id(live):
    link = _confirmed(state=SyncState.EXECUTED_PENDING_VERIFY)
    assert service.refresh_remote_status(link, client_factory=factory(FakeTransport(token_response(), _rows("Executed")))) == "Executed"
    link.refresh_from_db()
    assert (link.sync_state, link.remote_status, link.payment_id) == (SyncState.EXECUTED, RemoteStatus.EXECUTED, None)
    assert link.needs_review and "payment_id" in link.review_reason and link.executed_at is not None


@pytest.mark.parametrize("code", ["Slot Allocated", "Pending", "Something new"])
def test_status_refresh_without_executed_does_not_mark_executed(live, code):
    link = _confirmed(state=SyncState.EXECUTED_PENDING_VERIFY)
    service.refresh_remote_status(link, client_factory=factory(FakeTransport(token_response(), _rows(code))))
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTED_PENDING_VERIFY


def test_executed_status_does_not_skip_execute_for_a_merely_confirmed_link(live):
    link = _confirmed()
    service.refresh_remote_status(link, client_factory=factory(FakeTransport(token_response(), _rows("Executed"))))
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRMED  # read-only for links that were never executed by us


def test_repeated_processing_and_later_triggers_never_execute_twice(live):
    link = _confirmed()
    job = _queue(link)
    transport = FakeTransport(token_response(), json_response(EXECUTED_OK))
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.DONE
    assert service.process_job(job.pk, client_factory=factory(transport)) == "not-claimable"
    enqueue_fbr_execute(link.booking, trigger="complete_booking")
    assert service.enqueue_eligible_executions() == 0
    service.process_due_jobs(client_factory=factory(transport))
    assert transport.paths().count(contract.FBR_EXECUTE_PATH) == 1


@pytest.mark.parametrize("responses", [
    [TransportResponse(401, "")],  # the token endpoint refuses the credentials
    # A refused POST was not processed: repeated once with a fresh token; refused again, it waits.
    [token_response(), TransportResponse(401, ""), token_response(), TransportResponse(401, "")],
    [token_response(), json_response({"status": "ERROR", "message": "Token not found"}),
     token_response(), json_response({"status": "ERROR", "message": "Token not found"})],
    [token_response(), TransportResponse(403, "")],
])
def test_authentication_failure_waits_and_retries_later(live, responses):
    link = _confirmed()
    job = _queue(link)
    transport = FakeTransport(*responses)
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.RETRY_WAIT
    assert transport.responses == []
    assert transport.paths().count(contract.FBR_EXECUTE_PATH) <= 2
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_READY


@pytest.mark.parametrize("error", [ErrorCategory.CONNECT_FAILED, ErrorCategory.TLS])
def test_failure_before_sending_is_retried(live, error):
    link = _confirmed()
    job = _queue(link)
    transport = FakeTransport(token_response(), IstemError(error, "not sent"))
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.RETRY_WAIT
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_READY
    assert IstemApiAttempt.objects.get(operation=Operation.EXECUTE_FBR).outcome == AttemptOutcome.DEFINITE_FAILURE


@pytest.mark.parametrize("response,needle", [
    (TransportResponse(400, "{}"), ""),
    (json_response({"status": False, "message": "Invalid confirmation id"}), "Invalid confirmation id"),
    # [SPEC] invalid_confirmation_id / mismatched_confirmation_id examples
    (json_response({"status": "ERROR", "message": "Invalid confirmation_id"}), "Invalid confirmation_id"),
    (json_response({"status": "ERROR", "message": "confirmation_id does not belong to the given request_id"}),
     "does not belong to the given request_id"),
])
def test_explicit_rejection_is_failed_for_review(live, response, needle):
    link = _confirmed()
    job = _queue(link)
    before = _local_snapshot(link.booking)
    assert service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), response))) == JobState.BLOCKED
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_FAILED and link.needs_review
    assert needle in link.review_reason
    assert _local_snapshot(link.booking) == before


@pytest.mark.parametrize("response", [
    TransportResponse(200, ""),  # documented "200 Successful response" with no body: cannot be confirmed
    TransportResponse(200, "<html>ok</html>"),  # not JSON
    TransportResponse(200, json.dumps(["unexpected"])),  # JSON, wrong structure
    TransportResponse(200, json.dumps({"message": "done"})),  # no recognisable status
    TransportResponse(500, ""),
    TransportResponse(502, "upstream"),
    IstemError(ErrorCategory.READ_TIMEOUT, "t"),
    IstemError(ErrorCategory.CONNECTION_LOST, "c"),
])
def test_ambiguous_outcomes_are_uncertain_then_repeated_identically(live, response):
    link = _confirmed()
    job = _queue(link)
    before = _local_snapshot(link.booking)
    transport = FakeTransport(token_response(), response)
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.RETRY_WAIT
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_UNCERTAIN and link.needs_review and link.payment_id is None
    assert IstemApiAttempt.objects.get(operation=Operation.EXECUTE_FBR).outcome == AttemptOutcome.UNCERTAIN

    # Later completion signals never add a second job, and nothing is sent before the backoff expires.
    enqueue_fbr_execute(link.booking, trigger="complete_booking")
    assert service.enqueue_eligible_executions() == 0
    service.process_due_jobs(client_factory=factory(transport))
    assert transport.paths().count(contract.FBR_EXECUTE_PATH) == 1
    assert IstemOutboxJob.objects.filter(operation=Operation.EXECUTE_FBR).count() == 1

    # When due, the same job repeats exactly the same request; I-STEM answers "already executed" (idempotent).
    _make_due(job)
    again = FakeTransport(json_response(ALREADY_EXECUTED))
    assert service.process_job(job.pk, client_factory=factory(again)) == JobState.DONE
    assert again.calls[0]["data"] == transport.calls[1]["data"]
    link.refresh_from_db()
    assert (link.sync_state, link.payment_id, link.needs_review) == (SyncState.EXECUTED, "9981", False)
    assert _local_snapshot(link.booking) == before


def _make_due(job):
    IstemOutboxJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))


@pytest.mark.parametrize("body", [
    {**EXECUTED_OK, "confirmation_id": 44295},  # another confirmation
    {**EXECUTED_OK, "request_id": 55402},  # another request
    {k: v for k, v in EXECUTED_OK.items() if k != "payment_id"},  # success without payment_id
    {**EXECUTED_OK, "payment_id": "abc"},  # malformed payment_id
])
def test_success_with_mismatched_or_missing_identifiers_is_not_executed(live, body):
    link = _confirmed()
    job = _queue(link)
    assert service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(body)))) == JobState.RETRY_WAIT
    link.refresh_from_db()
    assert (link.sync_state, link.payment_id, link.executed_at) == (SyncState.EXECUTE_UNCERTAIN, None, None)


def test_uncertain_execute_is_not_repeated_after_the_final_amount_changed(live):
    link = _confirmed()
    job = _queue(link)
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    type(link.booking).objects.filter(pk=link.booking_id).update(total_charge=Decimal("1700.00"))
    _make_due(job)
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_UNCERTAIN and "refresh-status" in link.review_reason


def test_executed_status_read_while_an_identical_retry_is_queued_leaves_it_to_the_retry(live):
    link = _confirmed()
    job = _queue(link)
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    service.refresh_remote_status(link, client_factory=factory(FakeTransport(_rows("Executed"))))
    link.refresh_from_db()
    assert (link.sync_state, link.remote_status) == (SyncState.EXECUTE_UNCERTAIN, RemoteStatus.EXECUTED)
    _make_due(job)
    assert service.process_job(job.pk, client_factory=factory(FakeTransport(json_response(ALREADY_EXECUTED)))) == JobState.DONE
    link.refresh_from_db()
    assert (link.sync_state, link.payment_id) == (SyncState.EXECUTED, "9981")


@pytest.mark.parametrize("shift,field", [
    (timedelta(days=30), "allocation_date"),  # slot (allocation) in the future
    (timedelta(days=1), "allocation_date"),  # slot on the IST day after completion (still in the past)
])
def test_allocation_date_rules(live, shift, field):
    link = _confirmed()
    start = COMPLETED_AT + shift
    link.booking.daily_slots.update(start_datetime=start, end_datetime=start + timedelta(hours=1))
    result = build_execute_payload(link.booking, link)
    assert field in result.errors and field not in result.payload


def test_worker_lost_mid_execute_becomes_uncertain(live):
    link = _confirmed()
    job = _queue(link)
    IstemBookingLink.objects.filter(pk=link.pk).update(sync_state=SyncState.EXECUTE_IN_FLIGHT)
    IstemOutboxJob.objects.filter(pk=job.pk).update(state=JobState.CLAIMED, lease_expires_at=timezone.now() - timedelta(minutes=1))
    IstemApiAttempt.objects.create(job=job, booking_link=link, operation=Operation.EXECUTE_FBR,
                                   started_at=timezone.now() - timedelta(minutes=30))
    assert service.sweep_stale_in_flight() == 1
    link.refresh_from_db()
    job.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_UNCERTAIN
    assert job.state == JobState.RETRY_WAIT  # FBR Execute is idempotent: the same job repeats it identically


# --- uncertain-execute recovery --------------------------------------------------------------------------------------

def _uncertain_execute():
    """An uncertain FBR Execute whose automatic identical retries are used up (one attempt allowed)."""
    link = _confirmed()
    job = _queue(link)
    with override_settings(ISTEM_MAX_ATTEMPTS=1):
        state = service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    assert state == JobState.BLOCKED
    link.refresh_from_db()
    return link


def test_uncertain_execute_resolved_when_istem_reports_executed(live):
    link = _uncertain_execute()
    service.refresh_remote_status(link, client_factory=factory(FakeTransport(_rows("Executed"))))
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTED


def test_resubmission_needs_a_status_check_and_a_second_distinct_approver(live):
    link = _uncertain_execute()
    actor, approver = _admin("resolve_uncertain"), _admin("approve_resubmission")
    with pytest.raises(ValueError, match="refresh-status"):
        service.approve_execute_resubmission(link, actor=actor, approver=approver, **EVIDENCE)

    service.refresh_remote_status(link, client_factory=factory(FakeTransport(_rows("Slot Allocated"))))
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_UNCERTAIN
    with pytest.raises(PermissionError):
        service.approve_execute_resubmission(link, actor=actor, approver=actor, **EVIDENCE)
    with pytest.raises(PermissionError):
        service.approve_execute_resubmission(link, actor=_admin(), approver=approver, **EVIDENCE)
    with pytest.raises(PermissionError):
        service.approve_execute_resubmission(link, actor=actor, approver=_admin(), **EVIDENCE)
    with pytest.raises(ValueError):
        service.approve_execute_resubmission(link, actor=actor, approver=approver, **{**EVIDENCE, "reason": "too short"})
    with pytest.raises(ValueError):
        service.approve_execute_resubmission(link, actor=actor, approver=approver, **{**EVIDENCE, "evidence_reference": ""})

    job = service.approve_execute_resubmission(link, actor=actor, approver=approver, **EVIDENCE)
    assert job.operation == Operation.EXECUTE_FBR and job.state == JobState.PENDING
    link.refresh_from_db()
    assert link.sync_state == SyncState.EXECUTE_READY and link.reviewed_by_id == approver.pk


def test_executed_booking_cannot_be_resubmitted(live):
    link = _uncertain_execute()
    service.refresh_remote_status(link, client_factory=factory(FakeTransport(_rows("Executed"))))
    link.refresh_from_db()
    actor, approver = _admin("resolve_uncertain"), _admin("approve_resubmission")
    with pytest.raises(ValueError):
        service.approve_execute_resubmission(link, actor=actor, approver=approver, **EVIDENCE)
    assert not IstemOutboxJob.objects.filter(operation=Operation.EXECUTE_FBR, state=JobState.PENDING).exists()


def test_recover_command_requires_an_approver(live):
    link = _uncertain_execute()
    actor = _admin("resolve_uncertain")
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="--approver"):
        call_command("istem_recover", "approve-execute-resubmission", "--actor", actor.email, "--booking", str(link.booking_id))


# --- sanitisation ----------------------------------------------------------------------------------------------------

def test_no_credentials_tokens_or_payload_values_in_logs_or_records(live, caplog):
    caplog.set_level(logging.DEBUG, logger="iic_booking.istem")
    link = _confirmed()
    job = _queue(link)
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), TransportResponse(502, "upstream"))))
    dump = json.dumps([
        list(IstemApiAttempt.objects.values()), list(IstemBookingLink.objects.values()),
        list(IstemOutboxJob.objects.values()), list(IstemSyncEvent.objects.values()),
    ], default=str)
    assert FAKE_PASSWORD not in dump and FAKE_TOKEN not in dump
    logged = caplog.text
    assert FAKE_PASSWORD not in logged and FAKE_TOKEN not in logged
    assert "55401" not in logged and "44294" not in logged and "1500.50" not in logged
    assert "op=fbr_execute" in logged
    job.refresh_from_db()
    assert FAKE_TOKEN not in job.last_error_message and "upstream" not in job.last_error_message
