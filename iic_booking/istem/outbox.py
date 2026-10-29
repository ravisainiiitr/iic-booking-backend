"""Durable I-STEM work queue on ``IstemOutboxJob``: enqueue, claim, retry/block. No network here.

Local deduplication: at most one live job per ``dedupe_key`` (database constraint), and every job re-checks the
link state before acting, so repeated local events cannot start a second remote Create Booking. I-STEM is also
idempotent for identical Create/Confirm/Execute requests ([SPEC] v1.1.0), but the local records stay the source of
truth: an uncertain call is only repeated by its own job, with the same parameters.
"""

from __future__ import annotations

import os
import socket
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import conf
from .choices import LIVE_JOB_STATES, JobState, Operation, SyncState
from .eligibility import evaluate, evaluate_execute
from .models import IstemBookingLink, IstemOutboxJob, IstemSyncEvent, IstemUserLink

LEASE = timedelta(minutes=10)
MAX_BACKOFF_SECONDS = 3600


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


def record_event(*, booking_link=None, user_link=None, from_state="", to_state="", reason="", actor=None,
                 attempt=None, evidence_type="", evidence_reference="") -> IstemSyncEvent:
    return IstemSyncEvent.objects.create(
        booking_link=booking_link, user_link=user_link, from_state=from_state or "", to_state=to_state or "",
        reason=(reason or "")[:4000], actor=actor, attempt=attempt, evidence_type=evidence_type or "",
        evidence_reference=(evidence_reference or "")[:255],
    )


def add_job(operation: str, *, dedupe_key: str, trigger: str, booking_link=None, user_link=None) -> IstemOutboxJob:
    """The live job for ``dedupe_key``, creating it if none exists (duplicates collapse onto the existing one)."""
    existing = IstemOutboxJob.objects.filter(dedupe_key=dedupe_key, state__in=[s.value for s in LIVE_JOB_STATES]).first()
    if existing:
        return existing
    try:
        with transaction.atomic():
            return IstemOutboxJob.objects.create(
                operation=operation, dedupe_key=dedupe_key, trigger=trigger[:64],
                booking_link=booking_link, user_link=user_link, next_attempt_at=timezone.now(),
            )
    except IntegrityError:
        return IstemOutboxJob.objects.get(dedupe_key=dedupe_key, state__in=[s.value for s in LIVE_JOB_STATES])


def enqueue_user_link(user, trigger: str) -> IstemUserLink:
    with transaction.atomic():
        link, _ = IstemUserLink.objects.select_for_update().get_or_create(user=user)
        # A user waiting for their email OTP is already known to I-STEM; Create User would only repeat the answer.
        if link.state not in ("LINKED", "AWAITING_OTP"):
            add_job(Operation.ENSURE_USER, dedupe_key=f"ENSURE_USER:{user.pk}", trigger=trigger, user_link=link)
    return link


def enqueue_booking_sync(booking, trigger: str) -> IstemBookingLink | None:
    """Queue Create Booking for an eligible booking. Safe to call repeatedly and inside a booking transaction.

    Does nothing (and writes nothing) while the integration is off.
    """
    if conf.istem_mode() == conf.MODE_OFF:
        return None
    with transaction.atomic():
        link, _ = IstemBookingLink.objects.select_for_update().get_or_create(booking=booking)
        eligible, reason = evaluate(booking)
        if not eligible:
            if link.sync_state in (SyncState.NOT_ELIGIBLE, SyncState.BLOCKED_CONFIG) and link.eligibility_reason != reason[:64]:
                link.eligibility_reason = reason[:64]
                link.save(update_fields=["eligibility_reason", "updated_at"])
            return link
        if link.sync_state not in (SyncState.NOT_ELIGIBLE, SyncState.BLOCKED_CONFIG, SyncState.USER_PENDING, SyncState.CREATE_READY):
            return link  # already sent, in flight, uncertain or under review: never queue a second create
        if link.sync_state != SyncState.CREATE_READY:
            record_event(booking_link=link, from_state=link.sync_state, to_state=SyncState.CREATE_READY, reason=f"eligible ({trigger})")
            link.sync_state = SyncState.CREATE_READY
            link.eligibility_reason = ""
            link.save(update_fields=["sync_state", "eligibility_reason", "updated_at"])
        add_job(Operation.CREATE_BOOKING, dedupe_key=f"CREATE_BOOKING:{booking.pk}", trigger=trigger, booking_link=link)
    return link


def enqueue_fbr_execute(booking, trigger: str) -> IstemBookingLink | None:
    """Queue FBR Execute for a completed booking whose I-STEM booking is confirmed. Safe to call repeatedly.

    Writes nothing while the integration or FBR Execute is off, or when the booking was never sent to I-STEM. Only a
    CONFIRMED (or already EXECUTE_READY) link is queued: in-flight, uncertain, failed or executed links are left alone,
    so repeated completion signals can never start a second remote execution.
    """
    if conf.istem_mode() == conf.MODE_OFF or not conf.execute_enabled():
        return None
    with transaction.atomic():
        link = IstemBookingLink.objects.select_for_update().filter(booking=booking).first()
        if link is None:
            return None
        if link.sync_state not in (SyncState.CONFIRMED, SyncState.EXECUTE_READY):
            return link
        eligible, _ = evaluate_execute(booking, link)
        if not eligible:
            return link
        if link.sync_state != SyncState.EXECUTE_READY:
            record_event(booking_link=link, from_state=link.sync_state, to_state=SyncState.EXECUTE_READY, reason=f"booking completed ({trigger})")
            link.sync_state = SyncState.EXECUTE_READY
            link.save(update_fields=["sync_state", "updated_at"])
        add_job(Operation.EXECUTE_FBR, dedupe_key=f"EXECUTE_FBR:{booking.pk}", trigger=trigger, booking_link=link)
    return link


def claim_job(job_id: int) -> IstemOutboxJob | None:
    """Lock and claim one due job; None when it is not claimable (taken, finished or not yet due)."""
    now = timezone.now()
    with transaction.atomic():
        job = (
            IstemOutboxJob.objects.select_for_update(skip_locked=True)
            .filter(pk=job_id, state__in=[JobState.PENDING, JobState.RETRY_WAIT])
            .first()
        )
        if job is None or (job.next_attempt_at and job.next_attempt_at > now):
            return None
        job.state = JobState.CLAIMED
        job.claimed_at = now
        job.claimed_by = worker_id()
        job.lease_expires_at = now + LEASE
        job.save(update_fields=["state", "claimed_at", "claimed_by", "lease_expires_at", "updated_at"])
        return job


def due_job_ids(limit: int = 50) -> list[int]:
    now = timezone.now()
    return list(
        IstemOutboxJob.objects.filter(state__in=[JobState.PENDING, JobState.RETRY_WAIT], next_attempt_at__lte=now)
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )


def finish_job(job: IstemOutboxJob, state: str, *, category: str = "", message: str = "") -> None:
    job.state = state
    job.last_error_category = category or ""
    job.last_error_message = (message or "")[:2000]
    job.lease_expires_at = None
    if state in (JobState.DONE, JobState.BLOCKED, JobState.CANCELLED):
        job.finished_at = timezone.now()
    job.save(update_fields=["state", "last_error_category", "last_error_message", "lease_expires_at", "finished_at", "updated_at"])


def retry_or_block(job: IstemOutboxJob, *, category: str, message: str) -> str:
    """Bounded exponential backoff for failures known to be safe to send again; blocked after the last attempt."""
    if job.attempt_count >= conf.max_attempts():
        finish_job(job, JobState.BLOCKED, category=category, message=f"{message} (attempt limit reached)")
        return JobState.BLOCKED
    delay = min(60 * (2 ** max(0, job.attempt_count - 1)), MAX_BACKOFF_SECONDS)
    job.next_attempt_at = timezone.now() + timedelta(seconds=delay)
    job.save(update_fields=["next_attempt_at", "updated_at"])
    finish_job(job, JobState.RETRY_WAIT, category=category, message=message)
    return JobState.RETRY_WAIT
