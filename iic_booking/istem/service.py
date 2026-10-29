"""I-STEM job processing, read-only verification and administrator recovery.

Pattern for every remote call: (1) short transaction: lock the link, validate, record an IN_FLIGHT attempt;
(2) the HTTP call with no transaction open; (3) short transaction: record the outcome. A successful response's
identifiers are committed on the attempt before the link is updated, so they survive a failure of step (3).

Outcomes: not sent -> bounded retry; authentication refused -> bounded retry with a fresh token; definite refusal
-> blocked for review; possibly processed (timeout, lost connection, 5xx, malformed response, worker lost) ->
*_UNCERTAIN. Create Booking, Confirm Booking and FBR Execute are idempotent on I-STEM ([SPEC] v1.1.0), so an uncertain
one is repeated by the same job with exactly the same parameters (checked by fingerprint) under the bounded backoff;
if the parameters changed, or the attempts run out, it waits for an administrator. Create User may be retried too
(re-submitting a registered email/mobile returns the existing user).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Callable

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import conf
from . import contract
from .choices import (
    LIVE_JOB_STATES, AttemptOutcome, ErrorCategory, EvidenceType, JobState, Operation, RemoteStatus, SyncState, UserLinkState,
)
from .client import IstemClient, _as_remote_id
from .eligibility import evaluate, evaluate_execute
from .errors import IstemConfigError, IstemError
from .mapping import (
    booking_slot_window, build_confirm_payload, build_create_booking_payload, build_execute_payload,
    build_public_user_payload, collect_public_user_profile, istem_window_fits_day,
)
from .models import IstemApiAttempt, IstemBookingLink, IstemEquipmentMapping, IstemOutboxJob, IstemUserLink
from .outbox import (
    LEASE, add_job, claim_job, due_job_ids, enqueue_booking_sync, enqueue_fbr_execute, enqueue_user_link, finish_job,
    record_event, retry_or_block,
)
from .redaction import mask, redact

logger = logging.getLogger("iic_booking.istem")

ClientFactory = Callable[[], IstemClient]
REMOTE_STATUS_BY_CODE = {
    "Pending": RemoteStatus.PENDING,
    "Slot Allocated": RemoteStatus.SLOT_ALLOCATED,
    "Executed": RemoteStatus.EXECUTED,
    "Cancelled": RemoteStatus.CANCELLED,
    "Rejected": RemoteStatus.REJECTED,
}


def default_client() -> IstemClient:
    return IstemClient()


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _start_attempt(job, operation, *, booking_link=None, user_link=None, payload=None, endpoint="") -> IstemApiAttempt:
    if job is not None:
        job.attempt_count += 1
        job.save(update_fields=["attempt_count", "updated_at"])
    return IstemApiAttempt.objects.create(
        job=job, booking_link=booking_link, user_link=user_link, operation=operation, endpoint_name=endpoint,
        outcome=AttemptOutcome.IN_FLIGHT, request_fingerprint=_fingerprint(payload or {}),
        request_redacted=redact(payload or {}),
    )


def _finish_attempt(attempt, outcome, *, error: IstemError | None = None, response=None) -> None:
    attempt.outcome = outcome
    attempt.finished_at = timezone.now()
    attempt.duration_ms = int((attempt.finished_at - attempt.started_at).total_seconds() * 1000)
    if error is not None:
        attempt.error_category = error.category
        attempt.http_status = error.http_status
    if response is not None:
        attempt.response_redacted = redact(response)
    attempt.save()


def _outcome_for(error: IstemError) -> str:
    return AttemptOutcome.UNCERTAIN if error.possibly_processed else AttemptOutcome.DEFINITE_FAILURE


def _set_link(link, state, *, reason="", actor=None, attempt=None, review: str | None = None, **fields) -> None:
    previous = link.sync_state
    link.sync_state = state
    for key, value in fields.items():
        setattr(link, key, value)
    if review is not None:
        link.needs_review = bool(review)
        link.review_reason = review
    link.save()
    record_event(booking_link=link, from_state=previous, to_state=state, reason=reason, actor=actor, attempt=attempt)


# --- discovery ------------------------------------------------------------------------------------------------------

def enqueue_eligible(limit: int = 200) -> int:
    """Queue Create Booking for eligible bookings not yet sent. Run explicitly (management command); no scheduler."""
    from iic_booking.equipment.models import Booking

    from .eligibility import ELIGIBLE_STATUSES, MANUAL_FBR_SUBMITTED_STATUSES, sync_start_at

    if conf.istem_mode() == conf.MODE_OFF:
        return 0
    qs = Booking.objects.filter(charge_profile__require_istem_fbr=True, status__in=ELIGIBLE_STATUSES).filter(
        Q(istem_link__isnull=True)
        | Q(istem_link__sync_state__in=[SyncState.NOT_ELIGIBLE, SyncState.BLOCKED_CONFIG, SyncState.USER_PENDING])
    ).exclude(istem_fbr_status__in=MANUAL_FBR_SUBMITTED_STATUSES).exclude(istem_fbr_number__gt="")
    start = sync_start_at()
    if start is not None:
        qs = qs.filter(created_at__gte=start)
    queued = 0
    for booking in qs.order_by("pk")[:limit]:
        link = enqueue_booking_sync(booking, trigger="discovery")
        queued += int(link is not None and link.sync_state == SyncState.CREATE_READY)
    return queued


def enqueue_eligible_executions(limit: int = 200) -> int:
    """Queue FBR Execute for confirmed I-STEM bookings whose local booking is completed. Run explicitly; no scheduler."""
    if conf.istem_mode() == conf.MODE_OFF or not conf.execute_enabled():
        return 0
    links = (
        IstemBookingLink.objects.filter(
            sync_state__in=[SyncState.CONFIRMED, SyncState.EXECUTE_READY],
            booking__status="COMPLETED", booking__completed_at__isnull=False,
        )
        .select_related("booking")
        .order_by("pk")[:limit]
    )
    queued = 0
    for link in links:
        result = enqueue_fbr_execute(link.booking, trigger="discovery")
        queued += int(result is not None and result.sync_state == SyncState.EXECUTE_READY)
    return queued


# --- processing -----------------------------------------------------------------------------------------------------

def process_due_jobs(limit: int = 20, client_factory: ClientFactory = default_client) -> dict:
    sweep_stale_in_flight()
    results = {}
    for job_id in due_job_ids(limit):
        results[job_id] = process_job(job_id, client_factory=client_factory)
    return results


def process_job(job_id: int, client_factory: ClientFactory = default_client) -> str:
    """Process one job. Returns the resulting job state (or ``not-claimable`` / ``disabled``)."""
    if conf.istem_mode() == conf.MODE_OFF:
        return "disabled"
    job = claim_job(job_id)
    if job is None:
        return "not-claimable"
    runner = {
        Operation.CREATE_BOOKING: _run_create_booking,
        Operation.ENSURE_USER: _run_ensure_user,
        Operation.CONFIRM_BOOKING: _run_confirm_booking,
        Operation.EXECUTE_FBR: _run_execute_fbr,
    }.get(job.operation)
    if runner is None:
        finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message=f"operation {job.operation} is not processed in Stage 2")
        return JobState.BLOCKED
    return runner(job, client_factory)


SHADOW = object()  # returned by _client_or_block when nothing may be sent


def _client_or_block(job, client_factory):
    """The client in live mode; SHADOW in shadow mode; None (job blocked) when live settings are incomplete."""
    if conf.istem_mode() != conf.MODE_LIVE:
        return SHADOW
    try:
        return client_factory()
    except IstemConfigError as exc:
        finish_job(job, JobState.BLOCKED, category=exc.category, message=exc.message)
        return None


def _session_institute(job, client) -> tuple[str | None, str | None]:
    """(institute_id, job state). Authenticates outside any transaction; a failure here sent nothing, so the job is
    retried (bounded) and the link is left as it is."""
    try:
        return client.session_info().institute_id, None
    except IstemError as exc:
        return None, retry_or_block(job, category=exc.category, message=f"authentication: {exc.message}")


def equipment_remote_problem(mapping, institute_id: str | None) -> str:
    """Why the mapped I-STEM equipment may not be booked by this account ("" when it may): it must have been found in
    I-STEM's Equipment List, read with a token of the same institute as the one in use now."""
    if not institute_id:
        return "I-STEM's token response carries no institute_id, so the equipment cannot be checked"
    if mapping is None or mapping.remote_found is not True:
        return ("the I-STEM equipment_id was not found in I-STEM's Equipment List (Active equipment); run "
                "'istem_recover verify-equipment'")
    if str(mapping.remote_institute_id or "") != str(institute_id):
        return (f"the equipment was verified for institute {mapping.remote_institute_id or '-'}, but the token belongs to "
                f"institute {institute_id}; verify it again")
    return ""


def _latest_attempt(link, operation):
    return IstemApiAttempt.objects.filter(booking_link=link, operation=operation).order_by("-started_at", "-pk").first()


def _parameters_changed(link, operation, payload: dict) -> bool:
    """True when the last attempt of ``operation`` was uncertain and ``payload`` differs from what it sent. An
    uncertain call may only be repeated with exactly the same parameters (I-STEM's idempotency is per identical
    request), so a difference stops the automatic retry."""
    last = _latest_attempt(link, operation)
    return last is not None and last.outcome == AttemptOutcome.UNCERTAIN and last.request_fingerprint != _fingerprint(payload)


def _hold_uncertain(job, link, reason: str) -> str:
    """Stop an automatic retry of an uncertain operation and hand it to an administrator; the link keeps its state."""
    link.needs_review = True
    link.review_reason = reason[:4000]
    link.save(update_fields=["needs_review", "review_reason", "updated_at"])
    record_event(booking_link=link, from_state=link.sync_state, to_state=link.sync_state, reason=reason)
    finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFLICT, message=reason[:2000])
    return JobState.BLOCKED


def _retry_uncertain(job, link, attempt, exc: IstemError, *, state: str, what: str, manual: str) -> str:
    """Possibly processed. Create/Confirm/Execute are idempotent on I-STEM, so the same job is retried later with
    identical parameters (bounded backoff); after the last attempt the link stays UNCERTAIN for an administrator."""
    outcome = retry_or_block(job, category=exc.category, message=exc.message)
    if outcome == JobState.BLOCKED:
        review = f"{what} outcome still uncertain after {job.attempt_count} attempt(s). {manual}"
    else:
        review = f"{what} may have been applied on I-STEM; it will be repeated with identical parameters (idempotent)."
    _set_link(link, state, reason=exc.message, attempt=attempt, review=review)
    return outcome


CREATE_MANUAL_STEP = ("Confirm on the I-STEM portal or with I-STEM, then adopt the IDs ('adopt-ids') or record 'not "
                      "created' with a second approver ('mark-not-created').")


def _run_create_booking(job: IstemOutboxJob, client_factory) -> str:
    """Create Booking for a CREATE_READY link, or the identical repeat of an uncertain one (CREATE_UNCERTAIN)."""
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update(of=("self",)).select_related("booking__user", "booking__equipment", "booking__charge_profile").get(pk=job.booking_link_id)
        booking = link.booking
        if link.sync_state not in (SyncState.CREATE_READY, SyncState.CREATE_UNCERTAIN):
            finish_job(job, JobState.DONE, message=f"skipped: link is {link.sync_state}")
            return JobState.DONE
        repeat = link.sync_state == SyncState.CREATE_UNCERTAIN
        eligible, reason = evaluate(booking)
        if not eligible:
            if repeat:
                return _hold_uncertain(job, link, f"The booking is no longer eligible ({reason}) while an earlier Create "
                                                  f"Booking may exist on I-STEM. {CREATE_MANUAL_STEP}")
            _set_link(link, SyncState.NOT_ELIGIBLE, reason=reason, eligibility_reason=reason[:64])
            finish_job(job, JobState.CANCELLED, message=f"no longer eligible: {reason}")
            return JobState.CANCELLED
        user_link = IstemUserLink.objects.filter(user=booking.user).first()
        if user_link is None or user_link.state != UserLinkState.LINKED:
            if repeat:
                return _hold_uncertain(job, link, "The booking user's I-STEM link changed while an earlier Create Booking "
                                                  f"may exist on I-STEM. {CREATE_MANUAL_STEP}")
            # Parked until the user is linked; linking the user re-queues this booking (see _requeue_user_bookings).
            user_link = enqueue_user_link(booking.user, trigger="booking needs user")
            _set_link(link, SyncState.USER_PENDING, reason="waiting for the I-STEM user mapping", user_link=user_link)
            finish_job(job, JobState.CANCELLED, message="waiting for the I-STEM user mapping")
            return JobState.CANCELLED
        result = build_create_booking_payload(booking, user_link)
        if not result.ok:
            if repeat:
                return _hold_uncertain(job, link, "The Create Booking payload can no longer be built ("
                                                  + ", ".join(result.missing_fields()) + f"). {CREATE_MANUAL_STEP}")
            _set_link(link, SyncState.BLOCKED_CONFIG, reason="mapping incomplete", blocked_fields=result.missing_fields(),
                      review="; ".join(f"{k}: {v}" for k, v in sorted(result.errors.items()))[:4000])
            finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message="payload validation failed: " + ", ".join(result.missing_fields()))
            return JobState.BLOCKED
        payload = result.payload
        if repeat and _parameters_changed(link, Operation.CREATE_BOOKING, payload):
            return _hold_uncertain(job, link, "The booking changed after an uncertain Create Booking; repeating it would "
                                              f"not be the identical request I-STEM deduplicates. {CREATE_MANUAL_STEP}")
        link.create_payload = redact(payload)
        link.create_payload_hash = _fingerprint(payload)
        link.istem_equipment_id = payload["equipment_id"]
        link.user_link = user_link
        link.blocked_fields = []
        link.save()
        client = _client_or_block(job, client_factory)
        if client is SHADOW:
            record_event(booking_link=link, from_state=link.sync_state, to_state=link.sync_state, reason="shadow mode: payload valid, nothing sent")
            finish_job(job, JobState.DONE, message="shadow: payload validated, not sent")
            return JobState.DONE
        if client is None:
            return JobState.BLOCKED
        resume_state = link.sync_state

    # The account's institute comes from the token response; the network call runs outside any transaction.
    institute_id, job_state = _session_institute(job, client)
    if job_state is not None:
        return job_state

    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update(of=("self",)).select_related("booking").get(pk=link.pk)
        if link.sync_state != resume_state:
            finish_job(job, JobState.DONE, message=f"skipped: link became {link.sync_state}")
            return JobState.DONE
        mapping = IstemEquipmentMapping.objects.filter(equipment_id=link.booking.equipment_id).first()
        problem = equipment_remote_problem(mapping, institute_id)
        if problem:
            if resume_state == SyncState.CREATE_UNCERTAIN:
                return _hold_uncertain(job, link, f"Create Booking not repeated: {problem}. {CREATE_MANUAL_STEP}")
            _set_link(link, SyncState.BLOCKED_CONFIG, reason="equipment not verified on I-STEM", blocked_fields=["equipment_id"],
                      review=f"equipment_id: {problem}")
            finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message=f"equipment_id: {problem}"[:2000])
            return JobState.BLOCKED
        attempt = _start_attempt(job, Operation.CREATE_BOOKING, booking_link=link, payload=payload, endpoint="create-booking")
        _set_link(link, SyncState.CREATE_IN_FLIGHT, reason="repeating Create Booking (identical parameters)" if resume_state == SyncState.CREATE_UNCERTAIN else "sending Create Booking", attempt=attempt)

    try:
        response = client.create_booking(payload)
    except IstemError as exc:
        return _create_failed(job, link.pk, attempt, exc, resume_state=resume_state)

    with transaction.atomic():  # persist the remote identifiers first, on their own
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={"new_service_id": response.request_id, "fbr_no": response.fbr_no, "existing": response.existing})
    try:
        with transaction.atomic():
            link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
            _apply_create_success(link, attempt, response.request_id, response.fbr_no)
            finish_job(job, JobState.DONE)
    except IntegrityError:
        with transaction.atomic():
            link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
            link.needs_review = True
            link.review_reason = "I-STEM returned an identifier already linked to another booking; see the SUCCESS attempt."
            link.save(update_fields=["needs_review", "review_reason", "updated_at"])
            finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFLICT, message="remote identifier already linked locally")
        return JobState.BLOCKED
    except Exception:
        logger.exception("istem: could not record a successful Create Booking for link %s; recover from attempt %s", link.pk, attempt.pk)
        IstemOutboxJob.objects.filter(pk=job.pk).update(state=JobState.BLOCKED, last_error_category=ErrorCategory.CONFIG,
                                                         last_error_message=f"remote success recorded on attempt {attempt.pk}; link update failed, run recovery")
        return JobState.BLOCKED
    return JobState.DONE


def _apply_create_success(link, attempt, request_id: str, fbr_no: str | None) -> None:
    review = "" if fbr_no else "Create Booking returned no fbr_no (I-STEM states it should); obtain it from the I-STEM portal"
    _set_link(link, SyncState.CREATED, reason="Create Booking succeeded", attempt=attempt, review=review,
              request_id=request_id, fbr_no=fbr_no, remote_status=RemoteStatus.PENDING)


def _create_failed(job, link_pk, attempt, exc: IstemError, *, resume_state=SyncState.CREATE_READY) -> str:
    """Not sent / auth refused: back to the state the attempt started from (an uncertain create stays uncertain) and
    retry. Refused: failed for review. Possibly processed: CREATE_UNCERTAIN and an identical repeat."""
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link_pk)
        _finish_attempt(attempt, _outcome_for(exc), error=exc)
        if exc.not_sent or exc.auth_failure:
            _set_link(link, resume_state, reason=f"not accepted ({exc.category}); safe to send again", attempt=attempt)
            return retry_or_block(job, category=exc.category, message=exc.message)
        if exc.definite_failure:
            _set_link(link, SyncState.CREATE_FAILED, reason=exc.message, attempt=attempt, review=f"I-STEM refused the booking: {exc.message}")
            finish_job(job, JobState.BLOCKED, category=exc.category, message=exc.message)
            return JobState.BLOCKED
        return _retry_uncertain(job, link, attempt, exc, state=SyncState.CREATE_UNCERTAIN, what="Create Booking",
                                manual=CREATE_MANUAL_STEP)


def _identity_fingerprint(user) -> str:
    return hashlib.sha256(f"{(user.email or '').lower()}|{user.phone_number or ''}".encode("utf-8")).hexdigest()


def _link_user(user_link, public_user_id: str, *, reason: str, actor=None, attempt=None, evidence_type="", evidence_reference="") -> None:
    previous = user_link.state
    shared = IstemUserLink.objects.filter(public_user_id=public_user_id).exclude(pk=user_link.pk).exists()
    user_link.public_user_id = public_user_id
    user_link.identity_fingerprint = _identity_fingerprint(user_link.user)
    if shared:
        user_link.state = UserLinkState.NEEDS_REVIEW
        user_link.review_reason = "Another local account is already linked to this I-STEM user."
    else:
        user_link.state = UserLinkState.LINKED
        user_link.review_reason = ""
        user_link.candidate_public_user_id = ""
        user_link.candidate_email = ""
        user_link.linked_at = timezone.now()
    user_link.last_verified_at = timezone.now()
    user_link.save()
    record_event(user_link=user_link, from_state=previous, to_state=user_link.state, reason=reason, actor=actor,
                 attempt=attempt, evidence_type=evidence_type, evidence_reference=evidence_reference)
    if user_link.state == UserLinkState.LINKED:
        transaction.on_commit(lambda: _requeue_user_bookings(user_link.user_id))


def linked_elsewhere(user_link, public_user_id: str) -> bool:
    """True when another local account is already linked to ``public_user_id`` (one portal user per I-STEM user)."""
    return IstemUserLink.objects.filter(public_user_id=public_user_id, state=UserLinkState.LINKED).exclude(pk=user_link.pk).exists()


def _await_otp(user_link, public_user_id: str, email: str, *, attempt=None, profile: dict | None = None) -> None:
    """I-STEM already has this user: wait for the user to prove the I-STEM email with an OTP (``otp_link``)."""
    previous = user_link.state
    if linked_elsewhere(user_link, public_user_id):
        user_link.state = UserLinkState.NEEDS_REVIEW
        user_link.review_reason = (f"I-STEM reports this email/mobile is already registered as user {public_user_id}, "
                                   "which is linked to another portal account.")
        reason = "existing I-STEM user is linked to another account; OTP linking refused"
    else:
        user_link.state = UserLinkState.AWAITING_OTP
        user_link.candidate_public_user_id = public_user_id
        user_link.candidate_email = email
        user_link.candidate_profile = profile or {}  # applied only after the OTP proves the email
        user_link.review_reason = f"Already registered on I-STEM; waiting for the user's OTP sent to {mask(email)}."
        reason = "existing I-STEM user; waiting for the user's email OTP"
    user_link.save()
    record_event(user_link=user_link, from_state=previous, to_state=user_link.state, reason=reason, attempt=attempt)


def _requeue_user_bookings(user_id) -> None:
    for link in IstemBookingLink.objects.filter(booking__user_id=user_id, sync_state=SyncState.USER_PENDING).select_related("booking"):
        enqueue_booking_sync(link.booking, trigger="user linked")


def _block_user_payload(job, user_link, result) -> str:
    user_link.review_reason = ("Create User needs: " + "; ".join(f"{k}: {v}" for k, v in sorted(result.errors.items())))[:4000]
    user_link.save(update_fields=["review_reason", "updated_at"])
    finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message="Create User data incomplete: " + ", ".join(result.missing_fields()))
    return JobState.BLOCKED


def _run_ensure_user(job: IstemOutboxJob, client_factory) -> str:
    with transaction.atomic():
        user_link = IstemUserLink.objects.select_for_update(of=("self",)).select_related("user").get(pk=job.user_link_id)
        if user_link.state == UserLinkState.LINKED:
            finish_job(job, JobState.DONE, message="already linked")
            return JobState.DONE
        if user_link.state == UserLinkState.AWAITING_OTP:
            finish_job(job, JobState.DONE, message="waiting for the user's email OTP")
            return JobState.DONE
        if not conf.user_creation_enabled():
            user_link.review_reason = "Remote user creation is disabled (ISTEM_USER_CREATION_ENABLED); link the I-STEM user manually."
            user_link.save(update_fields=["review_reason", "updated_at"])
            finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message="remote user creation disabled")
            return JobState.BLOCKED
        # Local data first (no network): institute_id is only known from the token, checked below.
        local = build_public_user_payload(user_link.user, None)
        local.errors.pop("institute_id", None)
        if not local.ok:
            return _block_user_payload(job, user_link, local)
        client = _client_or_block(job, client_factory)
        if client is SHADOW:
            record_event(user_link=user_link, from_state=user_link.state, to_state=user_link.state,
                         reason="shadow mode: Create User payload valid (institute_id comes from the token), nothing sent")
            finish_job(job, JobState.DONE, message="shadow: payload validated, not sent")
            return JobState.DONE
        if client is None:
            return JobState.BLOCKED

    institute_id, job_state = _session_institute(job, client)
    if job_state is not None:
        return job_state

    with transaction.atomic():
        user_link = IstemUserLink.objects.select_for_update(of=("self",)).select_related("user").get(pk=job.user_link_id)
        if user_link.state == UserLinkState.LINKED:
            finish_job(job, JobState.DONE, message="already linked")
            return JobState.DONE
        result = build_public_user_payload(user_link.user, institute_id)
        if not result.ok:
            return _block_user_payload(job, user_link, result)
        attempt = _start_attempt(job, Operation.ENSURE_USER, user_link=user_link, payload=result.payload, endpoint="public-user")

    try:
        response = client.create_user(result.payload)
    except IstemError as exc:
        with transaction.atomic():
            _finish_attempt(attempt, _outcome_for(exc), error=exc)
            if exc.definite_failure:
                IstemUserLink.objects.filter(pk=user_link.pk).update(state=UserLinkState.NEEDS_REVIEW, review_reason=f"I-STEM refused Create User: {exc.message}"[:4000])
                finish_job(job, JobState.BLOCKED, category=exc.category, message=exc.message)
                return JobState.BLOCKED
            # Not sent, auth, or possibly processed: re-submitting the same email/mobile returns the existing user.
            return retry_or_block(job, category=exc.category, message=exc.message)

    with transaction.atomic():
        user_link = IstemUserLink.objects.select_for_update(of=("self",)).select_related("user").get(pk=user_link.pk)
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={"created": response.created, "user_id": response.user_id, "message": response.message,
                                                                   "user_email": response.email, "matched_on": response.matched_on})
        # The email registered on I-STEM: as returned, or the one just sent when I-STEM matched on the email.
        otp_email = response.email or (result.payload.get("user_email", "").strip().lower() if response.matched_on == "email" else "")
        if response.created:
            _link_user(user_link, response.user_id, reason="Create User succeeded", attempt=attempt)
        elif response.user_id and otp_email and conf.otp_linking_enabled():
            _await_otp(user_link, response.user_id, otp_email, attempt=attempt, profile=response.profile)
        elif response.user_id and conf.accept_duplicate_user_id():
            _link_user(user_link, response.user_id, reason="Create User reported an existing user; mapped to it", attempt=attempt)
        else:
            previous = user_link.state
            user_link.state = UserLinkState.NEEDS_REVIEW
            candidate = f" Candidate I-STEM user_id: {response.user_id}." if response.user_id else " No user_id could be read from the response."
            user_link.review_reason = ("I-STEM reports this email/mobile is already registered." + candidate +
                                       " Confirm the account and link it manually.")
            user_link.save(update_fields=["state", "review_reason", "updated_at"])
            record_event(user_link=user_link, from_state=previous, to_state=user_link.state, reason="duplicate-user response needs review", attempt=attempt)
        finish_job(job, JobState.DONE)
    return JobState.DONE


def request_confirmation(link: IstemBookingLink, *, actor, reason: str) -> IstemOutboxJob:
    """Queue Confirm Booking for a created booking. Explicit administrator action; off unless ISTEM_CONFIRM_ENABLED."""
    if not conf.confirm_enabled():
        raise IstemConfigError("Confirmation is disabled (ISTEM_CONFIRM_ENABLED): the confirmation trigger has not been agreed.")
    _require_perm(actor, "istem.request_confirm")
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        if link.sync_state != SyncState.CREATED or not link.request_id:
            raise IstemError(ErrorCategory.CONFIG, f"Confirmation needs a created booking with a request_id (link is {link.sync_state}).")
        if link.local_drift:
            raise IstemError(ErrorCategory.CONFLICT, "The local booking changed after Create Booking; reconcile it and run 'acknowledge-drift' first.")
        _set_link(link, SyncState.CONFIRM_READY, reason=reason or "confirmation requested", actor=actor)
        return add_job(Operation.CONFIRM_BOOKING, dedupe_key=f"CONFIRM_BOOKING:{link.booking_id}", trigger="admin", booking_link=link)


# Shown to administrators when Confirm Booking is held back because the local booking changed after Create Booking.
DRIFT_HINT = (
    "Confirm Booking was not sent. The I-STEM booking still holds the values from Create Booking. Bring it in line "
    "with the local booking (I-STEM portal or the I-STEM team), or accept the difference in writing, then record it "
    "with 'istem_recover acknowledge-drift' and request confirmation again."
)


def create_drift(booking, link, booking_payload: dict) -> list[str]:
    """Names (never values) of what changed since Create Booking; empty when the booking is unchanged.

    The full Create Booking payload is compared through its fingerprint (``create_payload_hash``, taken from the
    unmasked payload), so masked billing fields are covered too. Changed fields are named by comparing masked
    values; a fingerprint change with no visible difference is reported as a change to masked billing details.
    """
    changed = []
    pending = booking.charge_recalculation_pending_amount
    if pending is not None and pending != 0:
        changed.append("charge recalculation pending")
    if not link.create_payload_hash:
        return changed + ["no Create Booking baseline recorded"]
    if _fingerprint(booking_payload) == link.create_payload_hash:
        return changed
    stored, current = link.create_payload or {}, redact(booking_payload)
    fields = sorted(k for k in set(stored) | set(current) if stored.get(k) != current.get(k))
    return changed + (fields or ["billing details (masked)"])


def acknowledge_drift(link: IstemBookingLink, *, actor, evidence_type: str, evidence_reference: str,
                      reason: str) -> IstemBookingLink:
    """Accept the current local booking as the new Create Booking baseline after the I-STEM booking was reconciled.

    Sends nothing. Needs ``istem.reconcile`` plus evidence; the link returns to CREATED for a new confirmation request.
    """
    _require_perm(actor, "istem.reconcile")
    _require_evidence(evidence_type, evidence_reference, reason)
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update(of=("self",)).select_related("booking__user", "booking__equipment", "booking__charge_profile", "user_link").get(pk=link.pk)
        if not link.local_drift or link.sync_state != SyncState.CREATED:
            raise ValueError(f"no recorded drift to acknowledge (link is {link.sync_state})")
        booking = link.booking
        pending = booking.charge_recalculation_pending_amount
        if pending is not None and pending != 0:
            raise ValueError("a charge recalculation is still pending; settle it first")
        result = build_create_booking_payload(booking, link.user_link)
        if not result.ok:
            raise ValueError("the current booking cannot produce a complete payload: " + ", ".join(result.missing_fields()))
        previous = link.drift_reason
        link.create_payload = redact(result.payload)
        link.create_payload_hash = _fingerprint(result.payload)
        link.local_drift, link.drift_reason = False, ""
        link.needs_review, link.review_reason = False, ""
        link.reviewed_by, link.reviewed_at = actor, timezone.now()
        link.save()
        record_event(booking_link=link, from_state=link.sync_state, to_state=link.sync_state,
                     reason=f"drift acknowledged ({previous}): {reason}"[:4000], actor=actor,
                     evidence_type=evidence_type, evidence_reference=evidence_reference)
    return link


def _run_confirm_booking(job: IstemOutboxJob, client_factory) -> str:
    if not conf.confirm_enabled():
        finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message="confirmation disabled")
        return JobState.BLOCKED
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update(of=("self",)).select_related("booking__user", "booking__equipment", "booking__charge_profile", "user_link").get(pk=job.booking_link_id)
        if link.sync_state not in (SyncState.CONFIRM_READY, SyncState.CONFIRM_UNCERTAIN):
            finish_job(job, JobState.DONE, message=f"skipped: link is {link.sync_state}")
            return JobState.DONE
        repeat = link.sync_state == SyncState.CONFIRM_UNCERTAIN
        booking_payload = build_create_booking_payload(link.booking, link.user_link)
        # approved_on is the time confirmation was requested (this job's creation), so a repeat is identical.
        result = build_confirm_payload(link.booking, link, approved_on=job.created_at,
                                       booking_payload=booking_payload.payload if booking_payload.ok else None)
        # Change detection first: a moved slot or changed booking is drift (acknowledge-drift workflow), not a payload error.
        drift = create_drift(link.booking, link, booking_payload.payload) if booking_payload.ok else []
        if drift:
            summary = "Changed since Create Booking: " + ", ".join(drift)
            if repeat:
                return _hold_uncertain(job, link, f"{summary}. {CONFIRM_MANUAL_STEP}")
            _set_link(link, SyncState.CREATED, reason="Confirm blocked: " + summary, review=f"{summary}. {DRIFT_HINT}",
                      local_drift=True, drift_reason=summary[:4000])
            finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFLICT, message=summary[:2000])
            return JobState.BLOCKED
        errors = {**booking_payload.errors, **result.errors}
        if errors:
            summary = "Confirm Booking needs: " + "; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))
            if repeat:
                return _hold_uncertain(job, link, f"{summary}. {CONFIRM_MANUAL_STEP}")
            link.needs_review = True
            link.review_reason = summary[:4000]
            link.save(update_fields=["needs_review", "review_reason", "updated_at"])
            finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message="confirm payload validation failed")
            return JobState.BLOCKED
        if repeat and _parameters_changed(link, Operation.CONFIRM_BOOKING, result.payload):
            return _hold_uncertain(job, link, "Confirm Booking parameters changed after an uncertain attempt. " + CONFIRM_MANUAL_STEP)
        client = _client_or_block(job, client_factory)
        if client is SHADOW:
            finish_job(job, JobState.DONE, message="shadow: confirm payload validated, not sent")
            return JobState.DONE
        if client is None:
            return JobState.BLOCKED
        resume_state = link.sync_state
        attempt = _start_attempt(job, Operation.CONFIRM_BOOKING, booking_link=link, payload=result.payload, endpoint="confirm-booking")
        _set_link(link, SyncState.CONFIRM_IN_FLIGHT, reason="repeating Confirm Booking (identical parameters)" if repeat else "sending Confirm Booking", attempt=attempt)
        request_id = link.request_id

    try:
        response = client.confirm_booking(result.payload, request_id)
    except IstemError as exc:
        with transaction.atomic():
            link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
            _finish_attempt(attempt, _outcome_for(exc), error=exc)
            if exc.not_sent or exc.auth_failure:
                _set_link(link, resume_state, reason=f"not accepted ({exc.category}); safe to send again", attempt=attempt)
                return retry_or_block(job, category=exc.category, message=exc.message)
            if exc.definite_failure and contract.slot_occupied(exc.message):
                start, end = booking_slot_window(link.booking)
                shift = link.istem_slot_shift_hours + 1
                if start and end and istem_window_fits_day(start, end, shift):
                    # The covering hourly slot is taken on I-STEM: try the next hour of the same day.
                    _set_link(link, SyncState.CONFIRM_READY, attempt=attempt, istem_slot_shift_hours=shift,
                              reason=f"I-STEM slot occupied ({exc.message}); retrying {shift} hour(s) later")
                    return retry_or_block(job, category=ErrorCategory.CONFLICT, message=f"I-STEM slot occupied; moved {shift} hour(s) later")
                _set_link(link, SyncState.CONFIRM_FAILED, reason=exc.message, attempt=attempt,
                          review=f"No free hourly slot left on I-STEM that day ({exc.message}); reserve it on I-STEM by hand.")
                finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFLICT, message=exc.message)
                return JobState.BLOCKED
            if exc.definite_failure:
                _set_link(link, SyncState.CONFIRM_FAILED, reason=exc.message, attempt=attempt, review=f"I-STEM refused confirmation: {exc.message}")
                finish_job(job, JobState.BLOCKED, category=exc.category, message=exc.message)
                return JobState.BLOCKED
            return _retry_uncertain(job, link, attempt, exc, state=SyncState.CONFIRM_UNCERTAIN, what="Confirm Booking",
                                    manual=CONFIRM_MANUAL_STEP)

    with transaction.atomic():  # the confirmation_id is kept on the attempt first, on its own
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={"request_id": response.request_id, "confirmation_id": response.confirmation_id,
                                                                   "already_confirmed": response.already_confirmed})
    try:
        with transaction.atomic():
            link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
            reason = "Confirm Booking: already confirmed on I-STEM" if response.already_confirmed else "Confirm Booking succeeded"
            _set_link(link, SyncState.CONFIRMED, reason=reason, attempt=attempt, review="", confirmation_id=response.confirmation_id,
                      remote_status=RemoteStatus.SLOT_ALLOCATED, remote_status_raw="Slot Allocated", remote_status_checked_at=timezone.now())
            finish_job(job, JobState.DONE)
    except IntegrityError:
        with transaction.atomic():
            link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
            link.needs_review = True
            link.review_reason = f"I-STEM returned confirmation_id {response.confirmation_id}, already linked to another booking; see attempt {attempt.pk}."
            link.save(update_fields=["needs_review", "review_reason", "updated_at"])
            finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFLICT, message="confirmation_id already linked locally")
        return JobState.BLOCKED
    return JobState.DONE


CONFIRM_MANUAL_STEP = ("Check it with 'verify-confirmation' (Get Bookings); if I-STEM still shows Pending, a second "
                       "administrator may approve resubmission ('approve-confirm-resubmission').")
EXECUTE_MANUAL_STEP = ("Check it with 'refresh-status' (Get FBR Details); if it is not Executed, a second administrator "
                       "may approve resubmission ('approve-execute-resubmission').")


def _run_execute_fbr(job: IstemOutboxJob, client_factory) -> str:
    """FBR Execute. Never writes the local booking: a failure here cannot reverse or alter its completion. Executed
    only on I-STEM's documented answer (status SUCCESS with payment_id and the same request_id / confirmation_id)."""
    if not conf.execute_enabled():
        finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message="FBR Execute disabled (ISTEM_EXECUTE_ENABLED)")
        return JobState.BLOCKED
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update(of=("self",)).select_related("booking").get(pk=job.booking_link_id)
        booking = link.booking
        if link.sync_state not in (SyncState.EXECUTE_READY, SyncState.EXECUTE_UNCERTAIN):
            finish_job(job, JobState.DONE, message=f"skipped: link is {link.sync_state}")
            return JobState.DONE
        repeat = link.sync_state == SyncState.EXECUTE_UNCERTAIN
        eligible, reason = evaluate_execute(booking, link)
        if not eligible:
            if repeat:
                return _hold_uncertain(job, link, f"FBR Execute not repeated: no longer executable ({reason}). {EXECUTE_MANUAL_STEP}")
            _set_link(link, SyncState.CONFIRMED, reason=f"not executable: {reason}")
            finish_job(job, JobState.CANCELLED, message=f"no longer eligible: {reason}")
            return JobState.CANCELLED
        result = build_execute_payload(booking, link)
        if not result.ok:
            summary = "FBR Execute needs: " + "; ".join(f"{k}: {v}" for k, v in sorted(result.errors.items()))
            if repeat:
                return _hold_uncertain(job, link, f"{summary}. {EXECUTE_MANUAL_STEP}")
            link.needs_review = True
            link.blocked_fields = result.missing_fields()
            link.review_reason = summary[:4000]
            link.save(update_fields=["needs_review", "blocked_fields", "review_reason", "updated_at"])
            finish_job(job, JobState.BLOCKED, category=ErrorCategory.CONFIG, message="execute payload validation failed: " + ", ".join(result.missing_fields()))
            return JobState.BLOCKED
        if repeat and _parameters_changed(link, Operation.EXECUTE_FBR, result.payload):
            return _hold_uncertain(job, link, "FBR Execute parameters (dates or final amount) changed after an uncertain "
                                              f"attempt. {EXECUTE_MANUAL_STEP}")
        client = _client_or_block(job, client_factory)
        if client is SHADOW:
            record_event(booking_link=link, from_state=link.sync_state, to_state=link.sync_state, reason="shadow mode: FBR Execute payload valid, nothing sent")
            finish_job(job, JobState.DONE, message="shadow: execute payload validated, not sent")
            return JobState.DONE
        if client is None:
            return JobState.BLOCKED
        resume_state = link.sync_state
        attempt = _start_attempt(job, Operation.EXECUTE_FBR, booking_link=link, payload=result.payload, endpoint="fbr-execute")
        _set_link(link, SyncState.EXECUTE_IN_FLIGHT, reason="repeating FBR Execute (identical parameters)" if repeat else "sending FBR Execute", attempt=attempt)

    try:
        response = client.execute_fbr(result.payload)
    except IstemError as exc:
        with transaction.atomic():
            link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
            _finish_attempt(attempt, _outcome_for(exc), error=exc)
            if exc.not_sent or exc.auth_failure:
                _set_link(link, resume_state, reason=f"not accepted ({exc.category}); safe to send again", attempt=attempt)
                return retry_or_block(job, category=exc.category, message=exc.message)
            if exc.definite_failure:
                _set_link(link, SyncState.EXECUTE_FAILED, reason=exc.message, attempt=attempt, review=f"I-STEM refused FBR Execute: {exc.message}")
                finish_job(job, JobState.BLOCKED, category=exc.category, message=exc.message)
                return JobState.BLOCKED
            return _retry_uncertain(job, link, attempt, exc, state=SyncState.EXECUTE_UNCERTAIN, what="FBR Execute",
                                    manual=EXECUTE_MANUAL_STEP)

    with transaction.atomic():  # payment_id is kept on the attempt first, on its own
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={
            "payment_id": response.payment_id, "request_id": response.request_id,
            "confirmation_id": response.confirmation_id, "already_executed": response.already_executed,
        })
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        reason = "FBR Execute: already executed on I-STEM" if response.already_executed else "FBR Execute succeeded"
        _set_link(link, SyncState.EXECUTED, reason=reason, attempt=attempt, review="", payment_id=response.payment_id,
                  executed_at=link.executed_at or timezone.now(), remote_status=RemoteStatus.EXECUTED,
                  remote_status_raw="Executed", remote_status_checked_at=timezone.now())
        finish_job(job, JobState.DONE)
    return JobState.DONE


# --- read-only verification -----------------------------------------------------------------------------------------

def refresh_remote_status(link: IstemBookingLink, client_factory: ClientFactory = default_client) -> str | None:
    """Read the remote status via Get FBR Details (needs fbr_no; [SPEC] status_code "Executed" confirms FBR Execute).

    Updates remote_status. The only state change is completing an FBR Execute that is provisional or uncertain once
    I-STEM itself reports Executed. Get FBR Details carries no payment_id: when an identical FBR Execute retry is still
    queued it is left to run (I-STEM answers "already executed" with the payment_id); otherwise the link becomes
    EXECUTED and is flagged for review because the payment_id is unknown."""
    if not link.fbr_no:
        raise IstemError(ErrorCategory.CONFIG, "Get FBR Details needs the booking's fbr_no.")
    client = client_factory()
    with transaction.atomic():
        attempt = _start_attempt(None, Operation.GET_FBR_DETAILS, booking_link=link, payload={"fbr_no": link.fbr_no}, endpoint="get-fbr-details")
    try:
        rows = client.get_fbr_details(link.fbr_no)
    except IstemError as exc:
        with transaction.atomic():
            _finish_attempt(attempt, _outcome_for(exc), error=exc)
        raise
    match = [r for r in rows if str(r.get("fbr_no", "")).strip() == link.fbr_no]
    code = str(match[0].get("status_code", "")).strip() if len(match) == 1 else ""
    grand_total = str(match[0].get("grand_tot", "")).strip()[:32] if len(match) == 1 else ""
    with transaction.atomic():
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={"rows": len(rows), "matches": len(match), "status_code": code, "grand_tot": grand_total})
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        link.remote_status = REMOTE_STATUS_BY_CODE.get(code, RemoteStatus.UNKNOWN)
        link.remote_status_raw = code[:64]
        link.remote_status_checked_at = timezone.now()
        link.save(update_fields=["remote_status", "remote_status_raw", "remote_status_checked_at", "updated_at"])
        if link.remote_status == RemoteStatus.EXECUTED and link.sync_state in (SyncState.EXECUTED_PENDING_VERIFY, SyncState.EXECUTE_UNCERTAIN):
            retry_queued = IstemOutboxJob.objects.filter(
                booking_link=link, operation=Operation.EXECUTE_FBR, state__in=[s.value for s in LIVE_JOB_STATES]
            ).exists()
            if link.sync_state == SyncState.EXECUTE_UNCERTAIN and retry_queued and not link.payment_id:
                record_event(booking_link=link, from_state=link.sync_state, to_state=link.sync_state, attempt=attempt,
                             reason="Get FBR Details reports Executed; the queued identical FBR Execute will record the payment_id")
            else:
                review = "" if link.payment_id else (
                    "I-STEM reports Executed, but no payment_id was received (Get FBR Details does not return it). Record it "
                    "from the I-STEM portal if it is needed locally."
                )
                _set_link(link, SyncState.EXECUTED, reason="Get FBR Details reports Executed", attempt=attempt, review=review,
                          executed_at=link.executed_at or timezone.now())
    return code or None


CONFIRMATION_CONFIRMED = "confirmed"
CONFIRMATION_UNCONFIRMED = "unconfirmed"
CONFIRMATION_INCONCLUSIVE = "inconclusive"


def _matching_bookings(client, link, status_code: str):
    """(attempt, rows for this request_id) from Get Bookings with the documented fbr_no + status_code filters."""
    params = {"fbr_no": link.fbr_no, "status_code": status_code}
    with transaction.atomic():
        attempt = _start_attempt(None, Operation.GET_BOOKINGS, booking_link=link, payload=params, endpoint="get-bookings")
    try:
        rows = client.get_bookings(**params)
    except IstemError as exc:
        with transaction.atomic():
            _finish_attempt(attempt, _outcome_for(exc), error=exc)
        raise
    matches = [r for r in rows if _as_remote_id(r.get("request_id")) == link.request_id]
    confirmation_id = _as_remote_id(matches[0].get("confirmation_id")) if len(matches) == 1 else None
    with transaction.atomic():
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={"status_code": status_code, "rows": len(rows), "matches": len(matches), "confirmation_id": confirmation_id})
    return attempt, matches


def reconcile_confirmation(link: IstemBookingLink, client_factory: ClientFactory = default_client) -> tuple[str, str | None]:
    """Classify the remote confirmation state with Get Bookings; returns (outcome, confirmation_id).

    - confirmed: exactly one "Slot Allocated" row for this request_id carrying a valid confirmation_id. Resolves
      CONFIRM_UNCERTAIN / CONFIRMED_ID_MISSING to CONFIRMED.
    - unconfirmed: no "Slot Allocated" row, and exactly one "Pending" row for this request_id without a
      confirmation_id: positive evidence that I-STEM still has the booking as Pending. Recorded as remote_status
      PENDING; the link stays as it is and any resubmission still needs two administrators.
    - inconclusive: anything else (no row anywhere, several rows, a row without an ID). An absent row never counts as
      proof that confirmation did not happen. Recorded as remote_status UNKNOWN; the link stays as it is.

    A failed read raises and records nothing about the remote state.
    """
    if not link.fbr_no or not link.request_id:
        raise IstemError(ErrorCategory.CONFIG, "Verification needs both fbr_no and request_id.")
    client = client_factory()
    attempt, allocated = _matching_bookings(client, link, "Slot Allocated")
    confirmation_id = _as_remote_id(allocated[0].get("confirmation_id")) if len(allocated) == 1 else None
    if confirmation_id:
        outcome, remote, raw = CONFIRMATION_CONFIRMED, RemoteStatus.SLOT_ALLOCATED, "Slot Allocated"
    elif allocated:
        outcome, remote, raw = CONFIRMATION_INCONCLUSIVE, RemoteStatus.UNKNOWN, ""
    else:
        attempt, pending = _matching_bookings(client, link, "Pending")
        if len(pending) == 1 and not _as_remote_id(pending[0].get("confirmation_id")):
            outcome, remote, raw = CONFIRMATION_UNCONFIRMED, RemoteStatus.PENDING, "Pending"
        else:
            outcome, remote, raw = CONFIRMATION_INCONCLUSIVE, RemoteStatus.UNKNOWN, ""
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        link.remote_status, link.remote_status_raw, link.remote_status_checked_at = remote, raw, timezone.now()
        link.save(update_fields=["remote_status", "remote_status_raw", "remote_status_checked_at", "updated_at"])
        if outcome == CONFIRMATION_CONFIRMED and link.sync_state in (SyncState.CONFIRM_UNCERTAIN, SyncState.CONFIRMED_ID_MISSING):
            _set_link(link, SyncState.CONFIRMED, reason="confirmation_id read from Get Bookings", attempt=attempt, review="", confirmation_id=confirmation_id)
        else:
            record_event(booking_link=link, from_state=link.sync_state, to_state=link.sync_state,
                         reason=f"confirmation reconciliation: {outcome}", attempt=attempt)
    return outcome, confirmation_id


def verify_confirmation(link: IstemBookingLink, client_factory: ClientFactory = default_client) -> str | None:
    """The confirmation_id found by ``reconcile_confirmation``, or None."""
    return reconcile_confirmation(link, client_factory)[1]


def verify_equipment_mappings(client_factory: ClientFactory = default_client, *, equipment_ids=None) -> dict:
    """Check active equipment mappings against I-STEM's Equipment List for the token's institute. Read-only on I-STEM.

    The institute is the ``institute_id`` returned with the access token (never configured). Every page is read; if the
    listing cannot be read completely nothing is recorded. Each checked mapping gets remote_checked_at, remote_found,
    remote_equipment_name and remote_institute_id; the administrator's own ``verified_at`` is not touched.
    Returns {"institute_id", "user_entity", "listed", "found": [...], "missing": [...]} (local equipment codes).
    """
    client = client_factory()
    info = client.session_info()
    if not info.institute_id:
        raise IstemError(ErrorCategory.CONFIG, "I-STEM's token response carries no institute_id; equipment cannot be verified.")
    params = {"institute_id": info.institute_id, "limit": 100}
    with transaction.atomic():
        attempt = _start_attempt(None, Operation.LIST_EQUIPMENT, payload=params, endpoint="equipment-list")
    try:
        rows = client.list_all_equipment(institute_id=info.institute_id)
    except IstemError as exc:
        with transaction.atomic():
            _finish_attempt(attempt, _outcome_for(exc), error=exc)
        raise
    listed = {row["equipment_id"]: row["equipment_name"] for row in rows}
    mappings = IstemEquipmentMapping.objects.filter(is_active=True).select_related("equipment")
    if equipment_ids is not None:
        mappings = mappings.filter(equipment_id__in=list(equipment_ids))
    found, missing = [], []
    now = timezone.now()
    with transaction.atomic():
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={"institute_id": info.institute_id, "listed": len(listed)})
        for mapping in mappings.select_for_update(of=("self",)):
            istem_id = str(mapping.istem_equipment_id or "").strip()
            mapping.remote_checked_at = now
            mapping.remote_found = istem_id in listed
            mapping.remote_equipment_name = listed.get(istem_id, "")
            mapping.remote_institute_id = info.institute_id
            mapping.save(update_fields=["remote_checked_at", "remote_found", "remote_equipment_name", "remote_institute_id", "updated_at"])
            (found if mapping.remote_found else missing).append(mapping.equipment.code)
    logger.info("istem equipment verification: listed=%d found=%d missing=%d", len(listed), len(found), len(missing))
    return {"institute_id": info.institute_id, "user_entity": info.user_entity, "listed": len(listed),
            "found": sorted(found), "missing": sorted(missing)}


def verify_user_link(user, client_factory: ClientFactory = default_client) -> dict:
    """Check a local user's linked ``public_user_id`` against I-STEM's Public User Lookup. Read-only on I-STEM.

    The email and the mobile number are looked up separately (the spec does not say whether both together are
    matched with AND or OR). The link is confirmed only when every lookup made returns the linked id; then
    ``last_verified_at`` is set. A mismatch changes nothing locally and is reported for an administrator.
    Returns {"public_user_id", "by_email": [...] | None, "by_mobile": [...] | None, "matches"}.
    """
    link = IstemUserLink.objects.filter(user=user).first()
    if link is None or not link.public_user_id:
        raise ValueError(f"{user.email} has no linked I-STEM public_user_id")
    profile = collect_public_user_profile(user)
    lookups = {key: profile[key] for key in ("user_email", "user_contactno") if profile[key]}
    if not lookups:
        raise ValueError(f"{user.email} has no email or 10-digit mobile number to look up")
    client = client_factory()
    with transaction.atomic():
        attempt = _start_attempt(None, Operation.LOOKUP_USER, user_link=link, payload=lookups, endpoint="public-user-lookup")
    found = {}
    try:
        for key, value in lookups.items():
            found[key] = [row["user_id"] for row in client.lookup_public_user(**{key: value})]
    except IstemError as exc:
        with transaction.atomic():
            _finish_attempt(attempt, _outcome_for(exc), error=exc)
        raise
    matches = all(link.public_user_id in ids for ids in found.values())
    with transaction.atomic():
        _finish_attempt(attempt, AttemptOutcome.SUCCESS, response={"found": found, "matches": matches})
        if matches:
            IstemUserLink.objects.filter(pk=link.pk).update(last_verified_at=timezone.now(), updated_at=timezone.now())
    logger.info("istem user verification: user_link=%s matches=%s", link.pk, matches)
    return {"public_user_id": link.public_user_id, "by_email": found.get("user_email"),
            "by_mobile": found.get("user_contactno"), "matches": matches}


# --- recovery (administrators) ---------------------------------------------------------------------------------------

def _require_perm(actor, perm: str) -> None:
    if actor is None or not getattr(actor, "is_active", False) or not actor.has_perm(perm):
        raise PermissionError(f"{perm} is required")


def _require_evidence(evidence_type: str, evidence_reference: str, reason: str) -> None:
    if evidence_type not in EvidenceType.values or not (evidence_reference or "").strip():
        raise ValueError("an evidence type and reference are required")
    if len((reason or "").strip()) < 20:
        raise ValueError("a reason of at least 20 characters is required")


def recover_create_from_attempt(link: IstemBookingLink, *, actor) -> IstemBookingLink:
    """Apply a recorded successful Create Booking response whose link update failed."""
    _require_perm(actor, "istem.reconcile")
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        if link.sync_state not in (SyncState.CREATE_IN_FLIGHT, SyncState.CREATE_UNCERTAIN):
            raise ValueError(f"nothing to recover: link is {link.sync_state}")
        attempt = (IstemApiAttempt.objects.filter(booking_link=link, operation=Operation.CREATE_BOOKING, outcome=AttemptOutcome.SUCCESS)
                   .order_by("-started_at").first())
        request_id = (attempt.response_redacted or {}).get("new_service_id") if attempt else None
        if not request_id:
            raise ValueError("no recorded successful Create Booking response for this booking")
        _apply_create_success(link, attempt, str(request_id), (attempt.response_redacted or {}).get("fbr_no"))
        record_event(booking_link=link, from_state=SyncState.CREATED, to_state=SyncState.CREATED, reason="recovered from recorded response", actor=actor, attempt=attempt)
    return link


def adopt_remote_ids(link: IstemBookingLink, *, request_id: str, fbr_no: str, actor, evidence_type: str,
                     evidence_reference: str, reason: str) -> IstemBookingLink:
    """Record the I-STEM IDs of a booking confirmed (outside this system) to exist, after an uncertain create."""
    _require_perm(actor, "istem.link_manually")
    _require_evidence(evidence_type, evidence_reference, reason)
    if not str(request_id).isdigit() or not str(fbr_no).isalnum():
        raise ValueError("request_id must be numeric and fbr_no alphanumeric")
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        if link.sync_state not in (SyncState.CREATE_UNCERTAIN, SyncState.CREATE_FAILED, SyncState.MANUAL_REVIEW, SyncState.CREATE_IN_FLIGHT):
            raise ValueError(f"IDs can only be adopted after an uncertain or failed create (link is {link.sync_state})")
        if IstemBookingLink.objects.filter(request_id=str(request_id)).exclude(pk=link.pk).exists():
            raise ValueError("that request_id is already linked to another booking")
        previous = link.sync_state
        link.request_id, link.fbr_no = str(request_id), str(fbr_no)
        link.sync_state = SyncState.CREATED
        link.needs_review, link.review_reason = False, ""
        link.reviewed_by, link.reviewed_at = actor, timezone.now()
        link.save()
        record_event(booking_link=link, from_state=previous, to_state=SyncState.CREATED, reason=reason, actor=actor,
                     evidence_type=evidence_type, evidence_reference=evidence_reference)
    return link


def mark_not_created(link: IstemBookingLink, *, actor, approver, evidence_type: str, evidence_reference: str,
                     reason: str) -> IstemOutboxJob:
    """After an uncertain create, record (with a second administrator) that I-STEM has no such booking, and requeue."""
    _require_perm(actor, "istem.resolve_uncertain")
    _require_perm(approver, "istem.approve_resubmission")
    if approver.pk == actor.pk:
        raise PermissionError("the approver must be a different administrator")
    _require_evidence(evidence_type, evidence_reference, reason)
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        if link.sync_state != SyncState.CREATE_UNCERTAIN:
            raise ValueError(f"only an uncertain create can be resubmitted (link is {link.sync_state})")
        previous = link.sync_state
        link.sync_state = SyncState.CREATE_READY
        link.needs_review, link.review_reason = False, ""
        link.reviewed_by, link.reviewed_at = approver, timezone.now()
        link.save()
        record_event(booking_link=link, from_state=previous, to_state=SyncState.CREATE_READY,
                     reason=f"{reason} (approved by user {approver.pk})", actor=actor,
                     evidence_type=evidence_type, evidence_reference=evidence_reference)
        return add_job(Operation.CREATE_BOOKING, dedupe_key=f"CREATE_BOOKING:{link.booking_id}", trigger="resubmission approved", booking_link=link)


def approve_execute_resubmission(link: IstemBookingLink, *, actor, approver, evidence_type: str, evidence_reference: str,
                                 reason: str) -> IstemOutboxJob:
    """After an uncertain FBR Execute, re-queue it with a second administrator's approval.

    Allowed only when Get FBR Details was read after the uncertain attempt and did not report Executed.
    """
    _require_perm(actor, "istem.resolve_uncertain")
    _require_perm(approver, "istem.approve_resubmission")
    if approver.pk == actor.pk:
        raise PermissionError("the approver must be a different administrator")
    _require_evidence(evidence_type, evidence_reference, reason)
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        if link.sync_state != SyncState.EXECUTE_UNCERTAIN:
            raise ValueError(f"only an uncertain FBR Execute can be resubmitted (link is {link.sync_state})")
        last = (IstemApiAttempt.objects.filter(booking_link=link, operation=Operation.EXECUTE_FBR)
                .order_by("-started_at").first())
        if last is None or link.remote_status_checked_at is None or link.remote_status_checked_at <= last.started_at:
            raise ValueError("check the I-STEM status (refresh-status) after the uncertain attempt before resubmitting")
        if link.remote_status == RemoteStatus.EXECUTED:
            raise ValueError("I-STEM already reports this booking as Executed")
        previous = link.sync_state
        link.sync_state = SyncState.EXECUTE_READY
        link.needs_review, link.review_reason = False, ""
        link.reviewed_by, link.reviewed_at = approver, timezone.now()
        link.save()
        record_event(booking_link=link, from_state=previous, to_state=SyncState.EXECUTE_READY,
                     reason=f"{reason} (approved by user {approver.pk})", actor=actor,
                     evidence_type=evidence_type, evidence_reference=evidence_reference)
        return add_job(Operation.EXECUTE_FBR, dedupe_key=f"EXECUTE_FBR:{link.booking_id}", trigger="resubmission approved", booking_link=link)


def approve_confirm_resubmission(link: IstemBookingLink, *, actor, approver, evidence_type: str, evidence_reference: str,
                                 reason: str) -> IstemOutboxJob:
    """After an uncertain or refused Confirm Booking, re-queue it with a second administrator's approval.

    Allowed only when a status read made after the last Confirm attempt found the booking still Pending on I-STEM
    (``reconcile_confirmation`` "unconfirmed", or Get FBR Details "Pending"); an inconclusive or missing read is not
    enough. The change check runs again before anything is sent.
    """
    if not conf.confirm_enabled():
        raise IstemConfigError("Confirmation is disabled (ISTEM_CONFIRM_ENABLED).")
    _require_perm(actor, "istem.resolve_uncertain")
    _require_perm(approver, "istem.approve_resubmission")
    if approver.pk == actor.pk:
        raise PermissionError("the approver must be a different administrator")
    _require_evidence(evidence_type, evidence_reference, reason)
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
        if link.sync_state not in (SyncState.CONFIRM_UNCERTAIN, SyncState.CONFIRM_FAILED):
            raise ValueError(f"only an uncertain or refused confirmation can be resubmitted (link is {link.sync_state})")
        last = (IstemApiAttempt.objects.filter(booking_link=link, operation=Operation.CONFIRM_BOOKING)
                .order_by("-started_at").first())
        if last is None or link.remote_status_checked_at is None or link.remote_status_checked_at <= last.started_at:
            raise ValueError("reconcile the confirmation (verify-confirmation) after the last attempt before resubmitting")
        if link.remote_status != RemoteStatus.PENDING:
            raise ValueError(f"I-STEM was not shown to still be Pending (last read: {link.remote_status or 'none'})")
        previous = link.sync_state
        link.sync_state = SyncState.CONFIRM_READY
        link.needs_review, link.review_reason = False, ""
        link.reviewed_by, link.reviewed_at = approver, timezone.now()
        link.save()
        record_event(booking_link=link, from_state=previous, to_state=SyncState.CONFIRM_READY,
                     reason=f"{reason} (approved by user {approver.pk})", actor=actor,
                     evidence_type=evidence_type, evidence_reference=evidence_reference)
        return add_job(Operation.CONFIRM_BOOKING, dedupe_key=f"CONFIRM_BOOKING:{link.booking_id}", trigger="resubmission approved", booking_link=link)


def link_user_manually(user, *, public_user_id: str, actor, evidence_type: str, evidence_reference: str, reason: str) -> IstemUserLink:
    """Map a local user to an existing I-STEM public user confirmed by an administrator."""
    _require_perm(actor, "istem.link_manually")
    _require_evidence(evidence_type, evidence_reference, reason)
    if not str(public_user_id).isdigit():
        raise ValueError("public_user_id must be numeric")
    with transaction.atomic():
        user_link, _ = IstemUserLink.objects.select_for_update().get_or_create(user=user)
        _link_user(user_link, str(public_user_id), reason=reason, actor=actor, evidence_type=evidence_type, evidence_reference=evidence_reference)
    return user_link


def sweep_stale_in_flight() -> int:
    """Attempts left IN_FLIGHT past the lease (worker died mid-call) become UNCERTAIN. Create/Confirm/Execute are
    idempotent on I-STEM, so their job is retried later with identical parameters (bounded); any other operation is
    blocked for an administrator."""
    cutoff = timezone.now() - LEASE
    count = 0
    for attempt in IstemApiAttempt.objects.filter(outcome=AttemptOutcome.IN_FLIGHT, started_at__lt=cutoff):
        with transaction.atomic():
            attempt.outcome = AttemptOutcome.UNCERTAIN
            attempt.error_category = ErrorCategory.WORKER_LOST
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=["outcome", "error_category", "finished_at"])
            job = (IstemOutboxJob.objects.select_for_update().filter(pk=attempt.job_id, state=JobState.CLAIMED).first()
                   if attempt.job_id else None)
            retry = job is not None and attempt.operation in contract.IDEMPOTENT_OPERATIONS
            if job is not None:
                if retry:
                    retry_or_block(job, category=ErrorCategory.WORKER_LOST, message="worker stopped during the request")
                else:
                    finish_job(job, JobState.BLOCKED, category=ErrorCategory.WORKER_LOST, message="worker stopped during the request")
            link = attempt.booking_link
            if link is not None:
                link = IstemBookingLink.objects.select_for_update().get(pk=link.pk)
                target = {
                    SyncState.CREATE_IN_FLIGHT: SyncState.CREATE_UNCERTAIN,
                    SyncState.CONFIRM_IN_FLIGHT: SyncState.CONFIRM_UNCERTAIN,
                    SyncState.EXECUTE_IN_FLIGHT: SyncState.EXECUTE_UNCERTAIN,
                }.get(link.sync_state)
                if target:
                    review = ("The request may have reached I-STEM; it will be repeated with identical parameters (idempotent)."
                              if retry and job.state == JobState.RETRY_WAIT else
                              "The request may have reached I-STEM; reconcile before any resubmission.")
                    _set_link(link, target, reason="worker stopped during the request", attempt=attempt, review=review)
            count += 1
    # Claimed jobs whose lease expired without a request in flight (worker stopped before sending) are released.
    in_flight_jobs = IstemApiAttempt.objects.filter(outcome=AttemptOutcome.IN_FLIGHT, job__isnull=False).values("job_id")
    IstemOutboxJob.objects.filter(state=JobState.CLAIMED, lease_expires_at__lt=timezone.now()).exclude(
        pk__in=in_flight_jobs
    ).update(state=JobState.PENDING, lease_expires_at=None, claimed_by="")
    return count
