"""I-STEM live activity (14 Nov 2026): every call the portal makes to I-STEM, newest first, with how long it took from
the portal event to I-STEM's answer. Built for the end-to-end demonstration (portal on one half of the screen, I-STEM
on the other) and for the Main Administrator to see at once whether I-STEM keeps up.

Read-only. Only the Main Administrator sees it: it shows booking references, users' names and I-STEM identifiers.
"""

from __future__ import annotations

from datetime import timedelta
from statistics import median

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .choices import AttemptOutcome, JobState, Operation
from .models import IstemApiAttempt, IstemEquipmentDetails, IstemOutboxJob, IstemSyncEvent

# Calls that change something on I-STEM (look-ups, lists and status checks are left out of the feed).
SHOWN_OPERATIONS = (
    Operation.ENSURE_USER, Operation.CREATE_BOOKING, Operation.CONFIRM_BOOKING, Operation.EXECUTE_FBR,
    Operation.CANCEL_BOOKING, Operation.CANCEL_SUPERSEDED, Operation.RESCHEDULE_BOOKING,
)
CANCEL_TRIGGERS = ("portal cancellation", "retained cancellation")
MAX_EVENTS = 100
DEFAULT_WINDOW = timedelta(hours=24)
STATS_WINDOW = timedelta(days=7)


def _iso(moment) -> str | None:
    return moment.isoformat() if moment else None


def _seconds(start, end) -> float | None:
    if not start or not end:
        return None
    return round(max(0.0, (end - start).total_seconds()), 1)


def classify(operation: str, trigger: str = "", reason: str = "") -> tuple[str, str]:
    """(kind, label) of one I-STEM call, in the words used on the demo screen."""
    reason = (reason or "").lower()
    if operation == Operation.ENSURE_USER:
        if "existing" in reason or "already" in reason:
            return "user_existing", "Existing I-STEM user linked"
        return "user_created", "I-STEM user created"
    if operation == Operation.CREATE_BOOKING:
        if trigger == "slot moved":
            return "booking_moved", "Rescheduled: booking created for the new slot"
        return "booking_created", "Booking created on I-STEM (FBR)"
    if operation == Operation.CONFIRM_BOOKING:
        return "booking_allocated", "Slot allocated (booking confirmed)"
    if operation == Operation.EXECUTE_FBR:
        if trigger in CANCEL_TRIGGERS:
            return "booking_cancelled", "Cancelled: FBR executed for the fee kept"
        return "fbr_executed", "FBR executed (booking completed)"
    if operation == Operation.CANCEL_BOOKING:
        return "booking_cancelled", "Booking cancelled on I-STEM"
    if operation == Operation.CANCEL_SUPERSEDED:
        return "booking_moved", "Rescheduled: old slot cancelled on I-STEM"
    if operation == Operation.RESCHEDULE_BOOKING:
        return "booking_moved", "Booking rescheduled on I-STEM"
    return operation.lower(), Operation(operation).label if operation in Operation.values else operation


def _status(outcome: str) -> str:
    return {AttemptOutcome.SUCCESS: "done", AttemptOutcome.IN_FLIGHT: "sending",
            AttemptOutcome.DEFINITE_FAILURE: "refused", AttemptOutcome.UNCERTAIN: "uncertain"}.get(outcome, "unknown")


def _person(user) -> str:
    if user is None:
        return ""
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user) or ""


def _booking_fields(link) -> dict:
    if link is None:
        return {}
    booking = link.booking
    return {
        "booking_id": booking.pk,
        "booking_ref": booking.virtual_booking_id or str(booking.pk),
        "equipment": getattr(booking.equipment, "name", "") or "",
        "user": _person(booking.user),
        "fbr_no": link.fbr_no or "",
        "request_id": link.request_id or "",
        "confirmation_id": link.confirmation_id or "",
    }


def _attempt_event(attempt, reasons: dict) -> dict:
    job = attempt.job
    trigger = job.trigger if job else ""
    kind, label = classify(attempt.operation, trigger, reasons.get(attempt.pk, ""))
    portal_at = job.created_at if job else attempt.started_at
    row = {
        "id": f"a{attempt.pk}", "kind": kind, "label": label, "operation": attempt.operation, "trigger": trigger,
        "status": _status(attempt.outcome), "error": attempt.error_category or "",
        "detail": (reasons.get(attempt.pk) or (job.last_error_message if job and attempt.outcome != AttemptOutcome.SUCCESS else "") or "")[:300],
        "portal_at": _iso(portal_at), "sent_at": _iso(attempt.started_at), "answered_at": _iso(attempt.finished_at),
        "wait_seconds": _seconds(portal_at, attempt.started_at), "call_seconds": round((attempt.duration_ms or 0) / 1000, 1) if attempt.duration_ms is not None else None,
        "total_seconds": _seconds(portal_at, attempt.finished_at),
    }
    row.update(_booking_fields(attempt.booking_link))
    if attempt.user_link_id and not attempt.booking_link_id:
        row["user"] = _person(attempt.user_link.user)
        row["istem_user_id"] = attempt.user_link.public_user_id or ""
    return row


def _equipment_events(since) -> list[dict]:
    rows = []
    for details in (IstemEquipmentDetails.objects.filter(created_at_istem__gte=since).select_related("equipment")
                    .order_by("-created_at_istem")[:20]):
        rows.append({
            "id": f"e{details.pk}", "kind": "equipment_created", "label": "Equipment created on I-STEM", "status": "done",
            "operation": "CREATE_EQUIPMENT", "trigger": "admin", "error": "", "detail": "",
            "equipment": details.equipment.name, "istem_equipment_id": details.created_istem_equipment_id,
            "portal_at": _iso(details.created_at_istem), "sent_at": _iso(details.created_at_istem),
            "answered_at": _iso(details.created_at_istem), "wait_seconds": 0.0, "call_seconds": None, "total_seconds": None,
        })
    for details in (IstemEquipmentDetails.objects.filter(last_attempt_at__gte=since).exclude(last_error="")
                    .select_related("equipment").order_by("-last_attempt_at")[:20]):
        rows.append({
            "id": f"ef{details.pk}", "kind": "equipment_created", "label": "Equipment creation refused by I-STEM",
            "status": "refused", "operation": "CREATE_EQUIPMENT", "trigger": "admin", "error": "",
            "detail": details.last_error[:300], "equipment": details.equipment.name, "istem_equipment_id": "",
            "portal_at": _iso(details.last_attempt_at), "sent_at": _iso(details.last_attempt_at),
            "answered_at": _iso(details.last_attempt_at), "wait_seconds": 0.0, "call_seconds": None, "total_seconds": None,
        })
    return rows


def _waiting() -> list[dict]:
    """Jobs queued and not yet answered: what the demo audience sees move to the feed within seconds."""
    rows = []
    now = timezone.now()
    jobs = (IstemOutboxJob.objects.filter(operation__in=SHOWN_OPERATIONS,
                                          state__in=[JobState.PENDING, JobState.RETRY_WAIT, JobState.CLAIMED])
            .select_related("booking_link__booking__equipment", "booking_link__booking__user", "user_link__user")
            .order_by("created_at")[:30])
    for job in jobs:
        kind, label = classify(job.operation, job.trigger)
        row = {"id": f"j{job.pk}", "kind": kind, "label": label, "state": job.state, "trigger": job.trigger,
               "queued_at": _iso(job.created_at), "next_attempt_at": _iso(job.next_attempt_at),
               "age_seconds": _seconds(job.created_at, now), "error": job.last_error_category or "",
               "detail": (job.last_error_message or "")[:300]}
        row.update(_booking_fields(job.booking_link))
        if job.user_link_id and not job.booking_link_id:
            row["user"] = _person(job.user_link.user)
        rows.append(row)
    return rows


def _stats(now) -> list[dict]:
    """Per kind of call over the last 7 days: how long from the portal event to I-STEM's answer."""
    by_kind: dict[str, dict] = {}
    attempts = (IstemApiAttempt.objects.filter(operation__in=SHOWN_OPERATIONS, started_at__gte=now - STATS_WINDOW)
                .select_related("job").order_by("-started_at")[:2000])
    for attempt in attempts:
        kind, label = classify(attempt.operation, attempt.job.trigger if attempt.job else "")
        entry = by_kind.setdefault(kind, {"kind": kind, "label": label, "calls": 0, "failed": 0, "_totals": [], "_calls": []})
        entry["calls"] += 1
        if attempt.outcome != AttemptOutcome.SUCCESS:
            entry["failed"] += 1
            continue
        portal_at = attempt.job.created_at if attempt.job else attempt.started_at
        total = _seconds(portal_at, attempt.finished_at)
        if total is not None:
            entry["_totals"].append(total)
        if attempt.duration_ms is not None:
            entry["_calls"].append(attempt.duration_ms / 1000)
    rows = []
    for entry in by_kind.values():
        totals, calls = sorted(entry.pop("_totals")), entry.pop("_calls")
        entry["median_total_seconds"] = round(median(totals), 1) if totals else None
        entry["slowest_total_seconds"] = round(totals[-1], 1) if totals else None
        entry["median_call_seconds"] = round(median(calls), 1) if calls else None
        rows.append(entry)
    return sorted(rows, key=lambda r: r["label"])


def _peak(now) -> dict:
    from .realtime import peak_ends_at

    ends_at = peak_ends_at(now)
    return {"active": ends_at is not None, "ends_at": _iso(ends_at)}


def live_feed(since_text: str = "") -> dict:
    from . import conf
    from .realtime import realtime_enabled

    now = timezone.now()
    since = parse_datetime(since_text) if since_text else None
    if since is None or timezone.is_naive(since):
        since = now - DEFAULT_WINDOW
    attempts = list(
        IstemApiAttempt.objects.filter(operation__in=SHOWN_OPERATIONS, started_at__gte=since)
        .select_related("job", "booking_link__booking__equipment", "booking_link__booking__user", "user_link__user")
        .order_by("-started_at")[:MAX_EVENTS]
    )
    reasons = dict(
        IstemSyncEvent.objects.filter(attempt_id__in=[a.pk for a in attempts]).order_by("created_at")
        .values_list("attempt_id", "reason")
    )
    events = [_attempt_event(a, reasons) for a in attempts] + _equipment_events(since)
    events.sort(key=lambda e: e.get("sent_at") or "", reverse=True)
    return {
        "server_time": _iso(now),
        "since": _iso(since),
        "realtime": realtime_enabled(),
        "mode": conf.istem_mode(),
        "peak": _peak(now),
        "events": events[:MAX_EVENTS],
        "waiting": _waiting(),
        "stats": _stats(now),
    }
