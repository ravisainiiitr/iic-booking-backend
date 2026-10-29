"""Stage 4: manual-FBR duplicate prevention, change detection before Confirm Booking, and reconciliation of uncertain
confirmations, with a fake transport (no network)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db.models import F
from django.test import override_settings
from django.utils import timezone

from iic_booking.istem import contract, service
from iic_booking.istem.choices import CodeType, ErrorCategory, EvidenceType, JobState, Operation, RemoteStatus, SyncState
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.eligibility import evaluate
from iic_booking.istem.errors import IstemConfigError, IstemError
from iic_booking.istem.models import IstemBookingLink, IstemCodeMapping, IstemEquipmentMapping, IstemOutboxJob
from iic_booking.istem.outbox import enqueue_booking_sync
from iic_booking.users.tests.factories import UserFactory

from .support import (
    FakeTransport,
    apply_live,
    json_response,
    listing_response,
    link_user,
    make_booking,
    map_equipment,
    ready_booking,
    token_response,
)

pytestmark = pytest.mark.django_db

CREATED = {"status": "Sucess", "message": "Done", "file_path": "x.pdf", "new_service_id": 55401, "fbr_no": "1976125722"}
CONFIRMED = {"status": "SUCCESS", "message": "Booking confirmed", "result": {"request_id": "55401", "confirmation_id": "44294"}}
EVIDENCE = {"evidence_type": EvidenceType.RECONCILIATION, "evidence_reference": "get-bookings 2026-10-25",
            "reason": "I-STEM still lists the booking as Pending after the timed-out confirmation"}


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def live(settings):
    apply_live(settings, ISTEM_CONFIRM_ENABLED=True, ISTEM_CONFIRM_APPROVED_BY="OP_IITR")
    return settings


@pytest.fixture
def shadow(settings):
    settings.ISTEM_ENABLED, settings.ISTEM_MODE = True, "shadow"
    return settings


def factory(transport):
    return lambda: IstemClient(transport)


def _admin(*perms):
    user = UserFactory(admin_approved=True, is_staff=True)
    for codename in perms:
        user.user_permissions.add(Permission.objects.get(content_type__app_label="istem", codename=codename))
    return type(user).objects.get(pk=user.pk)  # drop the permission cache


def _created(booking=None):
    """A booking created on I-STEM through the real Create Booking path (token is cached afterwards)."""
    booking = booking or ready_booking()
    link = enqueue_booking_sync(booking, trigger="test")
    job = IstemOutboxJob.objects.get(booking_link=link, operation=Operation.CREATE_BOOKING, state=JobState.PENDING)
    service.process_job(job.pk, client_factory=factory(FakeTransport(token_response(), json_response(CREATED))))
    link.refresh_from_db()
    assert link.sync_state == SyncState.CREATED
    return link


def _confirm(link, transport):
    job = service.request_confirmation(link, actor=_admin("request_confirm"), reason="slot allocated")
    state = service.process_job(job.pk, client_factory=factory(transport))
    link.refresh_from_db()
    return state


def _uncertain_confirmation():
    """An uncertain confirmation whose automatic identical retries are used up (one attempt allowed), so it waits for
    an administrator."""
    link = _created()
    with override_settings(ISTEM_MAX_ATTEMPTS=1):
        assert _confirm(link, FakeTransport(IstemError(ErrorCategory.READ_TIMEOUT, "t"))) == JobState.BLOCKED
    assert link.sync_state == SyncState.CONFIRM_UNCERTAIN and link.needs_review
    return link


def _make_due(job_or_pk):
    pk = getattr(job_or_pk, "pk", job_or_pk)
    IstemOutboxJob.objects.filter(pk=pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))


def _bookings(*rows):
    return json_response({"status": "SUCCESS", "message": "Booking get successfully", "result": list(rows)})


# --- Fix 1: manual FBR duplicate prevention --------------------------------------------------------------------------

@pytest.mark.parametrize("number,status", [
    ("FBR-123", "PENDING_OIC"),
    ("FBR-123", "INVALID"),
    ("FBR-123", "EXECUTED"),
    ("FBR-123", "PENDING_FBR"),  # number entered while the status was not (yet) advanced
    ("", "PENDING_OIC"),
])
def test_booking_with_a_manual_fbr_is_never_created_on_istem(shadow, number, status):
    booking = ready_booking()
    type(booking).objects.filter(pk=booking.pk).update(istem_fbr_number=number, istem_fbr_status=status)
    booking.refresh_from_db()
    assert evaluate(booking) == (False, "manual FBR already submitted")
    assert service.enqueue_eligible() == 0
    enqueue_booking_sync(booking, trigger="test")
    assert not IstemOutboxJob.objects.filter(operation=Operation.CREATE_BOOKING).exists()


@pytest.mark.parametrize("status", [None, "PENDING_FBR"])
def test_booking_without_a_manual_fbr_is_still_eligible(shadow, status):
    booking = ready_booking()
    type(booking).objects.filter(pk=booking.pk).update(istem_fbr_number="", istem_fbr_status=status)
    booking.refresh_from_db()
    assert evaluate(booking) == (True, "")
    assert service.enqueue_eligible() == 1


def test_manual_fbr_entered_after_queueing_cancels_without_a_request(settings):
    apply_live(settings)
    booking = ready_booking()
    link = enqueue_booking_sync(booking, trigger="test")
    job = IstemOutboxJob.objects.get(booking_link=link, operation=Operation.CREATE_BOOKING)
    type(booking).objects.filter(pk=booking.pk).update(istem_fbr_number="FBR-123", istem_fbr_status="PENDING_OIC")
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.CANCELLED
    assert transport.calls == []
    link.refresh_from_db()
    assert link.sync_state == SyncState.NOT_ELIGIBLE and link.request_id is None


def test_manual_fbr_fields_are_not_modified(shadow):
    booking = ready_booking()
    type(booking).objects.filter(pk=booking.pk).update(istem_fbr_number="FBR-123", istem_fbr_status="PENDING_OIC")
    service.enqueue_eligible()
    booking.refresh_from_db()
    assert (booking.istem_fbr_number, booking.istem_fbr_status) == ("FBR-123", "PENDING_OIC")


# --- Fix 2: change detection before Confirm Booking ------------------------------------------------------------------

def test_unchanged_booking_is_confirmed(live):
    link = _created()
    transport = FakeTransport(json_response(CONFIRMED))
    assert _confirm(link, transport) == JobState.DONE
    assert transport.paths() == [contract.CONFIRM_BOOKING_PATH]
    assert (link.sync_state, link.confirmation_id, link.local_drift) == (SyncState.CONFIRMED, "44294", False)


def _shift_slots(booking):
    booking.daily_slots.update(start_datetime=F("start_datetime") + timedelta(hours=1), end_datetime=F("end_datetime") + timedelta(hours=1))


def _change_samples(booking):
    booking.input_values = {"A": "4"}
    booking.save(update_fields=["input_values"])


def _change_total(booking):
    booking.total_charge = Decimal("1600.00")
    booking.save(update_fields=["total_charge"])


def _change_service_type(booking):
    IstemCodeMapping.objects.filter(code_type=CodeType.SERVICE_TYPE).update(istem_value="Fabrication")


def _change_analysis_type(booking):
    IstemCodeMapping.objects.filter(code_type=CodeType.ANALYSIS_TYPE).update(istem_value="analysis2")


def _change_usage_type(booking):
    IstemCodeMapping.objects.filter(code_type__in=[CodeType.USAGE_TYPE, CodeType.USER_TYPE]).update(istem_value="Start-ups Company")


def _change_equipment_mapping(booking):
    IstemEquipmentMapping.objects.filter(equipment=booking.equipment).update(istem_equipment_id="12207")


def _change_billing_state(booking):
    booking.user.external_billing_profile.billing_state = "Uttar Pradesh"
    booking.user.external_billing_profile.save()


def _change_gstin(booking):
    booking.user.external_billing_profile.gstin = "09AAAAA1111A1Z5"  # same masked form as the original
    booking.user.external_billing_profile.save()


def _pending_recalculation(booking):
    booking.charge_recalculation_pending_amount = Decimal("100.00")
    booking.save(update_fields=["charge_recalculation_pending_amount"])


@pytest.mark.parametrize("change,expected", [
    (_shift_slots, {"preferred_timings_from", "preferred_timings_to"}),
    (_change_samples, {"booking_no_sample"}),
    (_change_total, {"total_est_amt"}),
    (_change_service_type, {"service_type"}),
    (_change_analysis_type, {"analysis_type"}),
    (_change_usage_type, {"usage_type", "user_type"}),
    (_change_equipment_mapping, {"equipment_id"}),
    (_change_billing_state, {"billing_state"}),
    (_change_gstin, {"billing details (masked)"}),
    (_pending_recalculation, {"charge recalculation pending"}),
])
def test_changed_booking_blocks_confirm_without_a_request(live, change, expected):
    link = _created()
    booking = link.booking
    change(booking)
    transport = FakeTransport()
    assert _confirm(link, transport) == JobState.BLOCKED
    assert transport.calls == []
    assert link.sync_state == SyncState.CREATED and link.local_drift and link.needs_review
    named = set(link.drift_reason.removeprefix("Changed since Create Booking: ").split(", "))
    assert expected <= named
    job = IstemOutboxJob.objects.get(operation=Operation.CONFIRM_BOOKING)
    assert job.state == JobState.BLOCKED and job.last_error_category == ErrorCategory.CONFLICT
    assert "acknowledge-drift" in link.review_reason


def test_drift_messages_name_fields_but_hold_no_values(live):
    link = _created()
    _change_total(link.booking)
    _change_billing_state(link.booking)
    _confirm(link, FakeTransport())
    job = IstemOutboxJob.objects.get(operation=Operation.CONFIRM_BOOKING)
    for text in (link.drift_reason, link.review_reason, job.last_error_message):
        assert "1600" not in text and "1500" not in text and "Uttar" not in text and "Uttarakhand" not in text


def test_link_without_a_create_baseline_is_not_confirmed(live):
    link = _created()
    IstemBookingLink.objects.filter(pk=link.pk).update(create_payload_hash="")
    link.refresh_from_db()
    assert _confirm(link, FakeTransport()) == JobState.BLOCKED
    assert "no Create Booking baseline recorded" in link.drift_reason


def test_drift_must_be_acknowledged_with_evidence_before_confirming_new_values(live):
    link = _created()
    _shift_slots(link.booking)
    _confirm(link, FakeTransport())
    with pytest.raises(IstemError):
        service.request_confirmation(link, actor=_admin("request_confirm"), reason="again")
    with pytest.raises(PermissionError):
        service.acknowledge_drift(link, actor=_admin(), **EVIDENCE)
    with pytest.raises(ValueError):
        service.acknowledge_drift(link, actor=_admin("reconcile"), **{**EVIDENCE, "reason": "short"})

    link = service.acknowledge_drift(link, actor=_admin("reconcile"), **{**EVIDENCE, "reason": "I-STEM portal booking rescheduled to the new slot"})
    assert (link.sync_state, link.local_drift, link.needs_review) == (SyncState.CREATED, False, False)
    IstemOutboxJob.objects.filter(operation=Operation.CONFIRM_BOOKING).delete()
    transport = FakeTransport(json_response(CONFIRMED))
    assert _confirm(link, transport) == JobState.DONE
    start = link.booking.daily_slots.order_by("start_datetime").first().start_datetime
    from iic_booking.istem.mapping import format_booking_datetime, istem_hour_window

    # I-STEM's hourly grid: the start of the hour the moved booking begins in.
    assert transport.calls[0]["data"]["allocated_timings_from"] == format_booking_datetime(istem_hour_window(start, start)[0])


def test_acknowledge_drift_refuses_when_there_is_no_drift_or_a_pending_recalculation(live):
    link = _created()
    with pytest.raises(ValueError, match="no recorded drift"):
        service.acknowledge_drift(link, actor=_admin("reconcile"), **EVIDENCE)
    _pending_recalculation(link.booking)
    _confirm(link, FakeTransport())
    with pytest.raises(ValueError, match="recalculation"):
        service.acknowledge_drift(link, actor=_admin("reconcile"), **EVIDENCE)


# --- Fix 3: reconciling uncertain confirmations ----------------------------------------------------------------------

def test_reconcile_confirmed(live):
    link = _uncertain_confirmation()
    transport = FakeTransport(_bookings({"request_id": "55401", "confirmation_id": "44294"}, {"request_id": "1", "confirmation_id": "2"}))
    assert service.reconcile_confirmation(link, client_factory=factory(transport)) == (service.CONFIRMATION_CONFIRMED, "44294")
    assert [c["params"]["status_code"] for c in transport.calls] == ["Slot Allocated"]
    link.refresh_from_db()
    assert (link.sync_state, link.confirmation_id, link.remote_status) == (SyncState.CONFIRMED, "44294", RemoteStatus.SLOT_ALLOCATED)


def test_reconcile_unconfirmed_keeps_the_link_unresolved(live):
    link = _uncertain_confirmation()
    transport = FakeTransport(_bookings(), _bookings({"request_id": "55401", "confirmation_id": None}))
    assert service.reconcile_confirmation(link, client_factory=factory(transport))[0] == service.CONFIRMATION_UNCONFIRMED
    assert [c["params"] for c in transport.calls] == [
        {"fbr_no": "1976125722", "status_code": "Slot Allocated"}, {"fbr_no": "1976125722", "status_code": "Pending"},
    ]
    link.refresh_from_db()
    assert (link.sync_state, link.remote_status, link.confirmation_id) == (SyncState.CONFIRM_UNCERTAIN, RemoteStatus.PENDING, None)
    assert not IstemOutboxJob.objects.filter(operation=Operation.CONFIRM_BOOKING, state=JobState.PENDING).exists()


@pytest.mark.parametrize("responses", [
    [_bookings(), _bookings()],  # absent everywhere: not proof of anything
    # Slot Allocated row without a confirmation_id, and Get All Bookings does not list it either
    [_bookings({"request_id": "55401"}), listing_response()],
    [_bookings({"request_id": "55401", "confirmation_id": "1"}, {"request_id": "55401", "confirmation_id": "2"})],
    [_bookings(), _bookings({"request_id": "55401"}, {"request_id": "55401"})],
    [_bookings(), _bookings({"request_id": "55401", "confirmation_id": "44294"})],  # Pending yet confirmed: contradictory
    [_bookings(), _bookings({"request_id": "99999"})],  # another booking only
])
def test_reconcile_inconclusive_keeps_the_link_unresolved(live, responses):
    link = _uncertain_confirmation()
    assert service.reconcile_confirmation(link, client_factory=factory(FakeTransport(*responses)))[0] == service.CONFIRMATION_INCONCLUSIVE
    link.refresh_from_db()
    assert (link.sync_state, link.remote_status) == (SyncState.CONFIRM_UNCERTAIN, RemoteStatus.UNKNOWN)


def test_failed_read_raises_and_records_no_remote_state(live):
    link = _uncertain_confirmation()
    before = (link.remote_status, link.remote_status_checked_at)
    with pytest.raises(IstemError):
        service.reconcile_confirmation(link, client_factory=factory(FakeTransport(IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    link.refresh_from_db()
    assert (link.remote_status, link.remote_status_checked_at) == before
    assert link.sync_state == SyncState.CONFIRM_UNCERTAIN


def test_exhausted_uncertain_confirmation_is_not_resent_automatically(live):
    link = _uncertain_confirmation()
    transport = FakeTransport()
    service.enqueue_eligible()
    enqueue_booking_sync(link.booking, trigger="again")
    service.process_due_jobs(client_factory=factory(transport))
    assert transport.calls == []
    with pytest.raises(IstemError):
        service.request_confirmation(link, actor=_admin("request_confirm"), reason="again")


def test_resubmission_needs_unconfirmed_evidence_after_the_attempt_and_two_admins(live):
    link = _uncertain_confirmation()
    actor, approver = _admin("resolve_uncertain"), _admin("approve_resubmission")
    with pytest.raises(ValueError, match="verify-confirmation"):
        service.approve_confirm_resubmission(link, actor=actor, approver=approver, **EVIDENCE)

    service.reconcile_confirmation(link, client_factory=factory(FakeTransport(_bookings(), _bookings())))
    link.refresh_from_db()
    with pytest.raises(ValueError, match="Pending"):  # inconclusive is not enough
        service.approve_confirm_resubmission(link, actor=actor, approver=approver, **EVIDENCE)

    service.reconcile_confirmation(link, client_factory=factory(FakeTransport(_bookings(), _bookings({"request_id": "55401"}))))
    link.refresh_from_db()
    with pytest.raises(PermissionError):
        service.approve_confirm_resubmission(link, actor=actor, approver=actor, **EVIDENCE)
    with pytest.raises(PermissionError):
        service.approve_confirm_resubmission(link, actor=_admin(), approver=approver, **EVIDENCE)
    with pytest.raises(PermissionError):
        service.approve_confirm_resubmission(link, actor=actor, approver=_admin(), **EVIDENCE)
    with pytest.raises(ValueError):
        service.approve_confirm_resubmission(link, actor=actor, approver=approver, **{**EVIDENCE, "evidence_reference": ""})

    job = service.approve_confirm_resubmission(link, actor=actor, approver=approver, **EVIDENCE)
    assert (job.operation, job.state) == (Operation.CONFIRM_BOOKING, JobState.PENDING)
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRM_READY and link.reviewed_by_id == approver.pk

    transport = FakeTransport(json_response(CONFIRMED))
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.DONE
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRMED


def test_evidence_from_before_the_last_attempt_does_not_count(live):
    link = _uncertain_confirmation()
    service.reconcile_confirmation(link, client_factory=factory(FakeTransport(_bookings(), _bookings({"request_id": "55401"}))))
    link.refresh_from_db()
    actor, approver = _admin("resolve_uncertain"), _admin("approve_resubmission")
    job = service.approve_confirm_resubmission(link, actor=actor, approver=approver, **EVIDENCE)
    service.process_job(job.pk, client_factory=factory(FakeTransport(IstemError(ErrorCategory.READ_TIMEOUT, "t"))))
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRM_UNCERTAIN
    with pytest.raises(ValueError, match="verify-confirmation"):
        service.approve_confirm_resubmission(link, actor=actor, approver=approver, **EVIDENCE)


def test_resubmitted_confirmation_still_runs_the_change_check(live):
    link = _uncertain_confirmation()
    service.reconcile_confirmation(link, client_factory=factory(FakeTransport(_bookings(), _bookings({"request_id": "55401"}))))
    link.refresh_from_db()
    job = service.approve_confirm_resubmission(link, actor=_admin("resolve_uncertain"), approver=_admin("approve_resubmission"), **EVIDENCE)
    _change_total(link.booking)
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []
    link.refresh_from_db()
    assert link.local_drift


@pytest.mark.parametrize("state", [SyncState.CREATED, SyncState.CONFIRMED, SyncState.CONFIRMED_ID_MISSING, SyncState.CONFIRM_IN_FLIGHT])
def test_resubmission_only_from_uncertain_or_refused(live, state):
    link = _created()
    IstemBookingLink.objects.filter(pk=link.pk).update(sync_state=state)
    link.refresh_from_db()
    with pytest.raises(ValueError, match="only an uncertain or refused"):
        service.approve_confirm_resubmission(link, actor=_admin("resolve_uncertain"), approver=_admin("approve_resubmission"), **EVIDENCE)


def test_resubmission_refused_while_confirmation_is_disabled(live):
    link = _uncertain_confirmation()
    live.ISTEM_CONFIRM_ENABLED = False
    with pytest.raises(IstemConfigError):
        service.approve_confirm_resubmission(link, actor=_admin("resolve_uncertain"), approver=_admin("approve_resubmission"), **EVIDENCE)


def test_verify_confirmation_command_reports_the_outcome(live, monkeypatch, capsys):
    link = _uncertain_confirmation()
    transport = FakeTransport(_bookings(), _bookings())
    monkeypatch.setattr(service, "IstemClient", lambda: IstemClient(transport))  # used by service.default_client
    actor = _admin("reconcile")
    call_command("istem_recover", "verify-confirmation", "--actor", actor.email, "--booking", str(link.booking_id))
    assert "confirmation: inconclusive" in capsys.readouterr().out


# --- Confirm Booking per the OpenAPI spec (v1.1.0): five fields, idempotent repeat, confirmation_id persisted ----------

ALREADY_CONFIRMED = {"status": "Sucess", "message": "Booking is already confirmed", "confirmation_id": 54210, "result": {}}


def test_confirm_sends_the_required_fields_with_the_full_booking(live):
    # [STAGING] only a confirmation that re-sends the full booking is recorded with its amount and listed.
    link = _created()
    transport = FakeTransport(json_response(CONFIRMED))
    assert _confirm(link, transport) == JobState.DONE
    sent = transport.calls[0]["data"]
    assert set(contract.CONFIRM_MANDATORY_FIELDS) <= set(sent)
    assert set(contract.CREATE_BOOKING_MANDATORY_FIELDS) <= set(sent)
    assert sent["request_id"] == "55401" and sent["approved_by"] == "OP_IITR"
    assert sent["booking_status"] == "Slot Allocated"
    # The live values are sent, never the masked copy kept on the link.
    assert "***" not in sent["billing_address"] and "***" not in sent["booking_gstn"]
    assert (link.remote_status, link.confirmation_id) == (RemoteStatus.SLOT_ALLOCATED, "44294")


def test_uncertain_confirmation_is_repeated_with_identical_parameters_then_confirmed(live):
    link = _created()
    job = service.request_confirmation(link, actor=_admin("request_confirm"), reason="slot allocated")
    first = FakeTransport(IstemError(ErrorCategory.READ_TIMEOUT, "t"))
    assert service.process_job(job.pk, client_factory=factory(first)) == JobState.RETRY_WAIT
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRM_UNCERTAIN and "identical parameters" in link.review_reason
    assert service.process_job(job.pk, client_factory=factory(FakeTransport())) == "not-claimable"  # not yet due

    _make_due(job)
    second = FakeTransport(json_response(ALREADY_CONFIRMED))
    assert service.process_job(job.pk, client_factory=factory(second)) == JobState.DONE
    assert second.calls[0]["data"] == first.calls[0]["data"]  # same request_id, approved_on and timings
    link.refresh_from_db()
    assert (link.sync_state, link.confirmation_id, link.needs_review) == (SyncState.CONFIRMED, "54210", False)
    attempts = list(link.attempts.filter(operation=Operation.CONFIRM_BOOKING).order_by("started_at"))
    assert [a.outcome for a in attempts] == ["UNCERTAIN", "SUCCESS"]
    assert attempts[0].request_fingerprint == attempts[1].request_fingerprint
    assert attempts[1].response_redacted["already_confirmed"] is True
    assert IstemOutboxJob.objects.filter(operation=Operation.CONFIRM_BOOKING).count() == 1


def test_uncertain_confirmation_is_not_repeated_after_the_booking_changed(live):
    link = _created()
    job = service.request_confirmation(link, actor=_admin("request_confirm"), reason="slot allocated")
    service.process_job(job.pk, client_factory=factory(FakeTransport(IstemError(ErrorCategory.CONNECTION_LOST, "lost"))))
    _shift_slots(link.booking)
    _make_due(job)
    transport = FakeTransport()
    assert service.process_job(job.pk, client_factory=factory(transport)) == JobState.BLOCKED
    assert transport.calls == []
    link.refresh_from_db()
    assert link.sync_state == SyncState.CONFIRM_UNCERTAIN and link.needs_review
    assert "verify-confirmation" in link.review_reason


def test_confirm_answer_for_another_request_is_uncertain_not_confirmed(live):
    link = _created()
    other = {**CONFIRMED, "result": {"request_id": "99999"}, "confirmation_id": "44294"}
    assert _confirm(link, FakeTransport(json_response(other))) == JobState.RETRY_WAIT
    assert (link.sync_state, link.confirmation_id) == (SyncState.CONFIRM_UNCERTAIN, None)


def test_confirmation_id_already_used_by_another_booking_is_held_for_review(live):
    first = _created()
    IstemBookingLink.objects.filter(pk=first.pk).update(request_id="55400", fbr_no="1976125700", confirmation_id="44294",
                                                        sync_state=SyncState.CONFIRMED)
    clear_token_cache()
    booking = make_booking()  # the global code mappings already exist from the first booking
    map_equipment(booking.equipment)
    IstemCodeMapping.objects.create(code_type=CodeType.ANALYSIS_TYPE, local_key="HOUR", istem_value="analysis1", equipment=booking.equipment)
    link_user(booking.user)
    second = _created(booking)  # receives request_id 55401
    assert _confirm(second, FakeTransport(json_response(CONFIRMED))) == JobState.BLOCKED
    assert second.needs_review and "already linked" in second.review_reason
    attempt = second.attempts.get(operation=Operation.CONFIRM_BOOKING)
    assert attempt.outcome == "SUCCESS" and attempt.response_redacted["confirmation_id"] == "44294"
