"""Audit trail behind Booking Event History: who changed a booking, in which role, why and from where.

``create_booking_event`` stamps every event with ``metadata["audit"]`` (``audit_metadata``); inside a Celery
task an event with no ``created_by`` is the System's. ``Booking.save`` reports status changes to
``note_status_change``; a change that no code path recorded with its own event is logged as "Status changed"
once the transaction commits, attributed to the signed-in request user (``BookingAuditMiddleware``) or the
System, so the history never skips a status.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

AUDIT_KEY = "audit"

SOURCE_WEB = "WEB"
SOURCE_SCHEDULED_JOB = "SCHEDULED_JOB"
SOURCE_BACKGROUND_JOB = "BACKGROUND_JOB"
SOURCE_SYSTEM = "SYSTEM"
JOB_SOURCES = frozenset({SOURCE_SCHEDULED_JOB, SOURCE_BACKGROUND_JOB})

ROLE_USER = "USER"
ROLE_SUPERVISOR = "SUPERVISOR"
ROLE_OIC = "OIC"
ROLE_TEMP_OIC = "TEMP_OIC"
ROLE_LAB_OPERATOR = "LAB_OPERATOR"
ROLE_DEPT_ADMIN = "DEPT_ADMIN"
ROLE_MAIN_ADMIN = "MAIN_ADMIN"
ROLE_OTHER_STAFF = "OTHER_STAFF"
ROLE_OTHER_USER = "OTHER_USER"
ROLE_SYSTEM = "SYSTEM"
ROLE_UNKNOWN = "UNKNOWN"

ROLE_LABELS = {
    ROLE_USER: "Booking user",
    ROLE_SUPERVISOR: "Supervisor",
    ROLE_OIC: "Officer In Charge",
    ROLE_TEMP_OIC: "Temporary OIC",
    ROLE_LAB_OPERATOR: "Lab Operator",
    ROLE_DEPT_ADMIN: "Department Administrator",
    ROLE_MAIN_ADMIN: "Main Administrator",
    ROLE_OTHER_STAFF: "Staff",
    ROLE_OTHER_USER: "Another user",
    ROLE_SYSTEM: "System (automatic)",
    ROLE_UNKNOWN: "Not recorded",
}
USER_SIDE_ROLES = frozenset({ROLE_USER, ROLE_SUPERVISOR, ROLE_OTHER_USER})

SCHEDULED_JOB_LABELS = {
    "equipment.process_maintenance_booking_deadlines": "5-day disruption decision deadline",
    "equipment.auto_mark_operator_absent_disruption_after_booking_end": "Operator-absent safeguard",
    "equipment.daily_operator_on_leave_sweep": "Operator-on-leave sweep",
    "equipment.daily_under_maintenance_sweep": "Daily under-maintenance sweep",
}

REASON_OPERATOR_ABSENT = "OPERATOR_ABSENT"
REASON_UNDER_MAINTENANCE = "UNDER_MAINTENANCE"
REASON_ANALYSIS_NOT_POSSIBLE = "ANALYSIS_NOT_POSSIBLE"
REASON_REFUND_AFTER_DISRUPTION = "REFUND_AFTER_DISRUPTION"
REASON_CANCEL_AFTER_DISRUPTION = "CANCEL_AFTER_DISRUPTION"
REASON_RESCHEDULE_AFTER_DISRUPTION = "RESCHEDULE_AFTER_DISRUPTION"
REASON_DISRUPTION_DEADLINE = "DISRUPTION_DEADLINE"
REASON_STATUS_CHANGE = "STATUS_CHANGE"

REASON_LABELS = {
    REASON_OPERATOR_ABSENT: "Operator Absent",
    REASON_UNDER_MAINTENANCE: "Under Maintenance",
    REASON_ANALYSIS_NOT_POSSIBLE: "Analysis Not Possible",
    REASON_REFUND_AFTER_DISRUPTION: "Refund after lab disruption",
    REASON_CANCEL_AFTER_DISRUPTION: "Cancelled after lab disruption",
    REASON_RESCHEDULE_AFTER_DISRUPTION: "Rescheduled after lab disruption",
    REASON_DISRUPTION_DEADLINE: "No choice within 5 days — refunded automatically",
    REASON_STATUS_CHANGE: "Status changed",
}

DISRUPTION_KIND_REASONS = {
    "MAINTENANCE": REASON_UNDER_MAINTENANCE,
    "OPERATOR_ABSENT": REASON_OPERATOR_ABSENT,
    "OTHER_DISRUPTION": REASON_ANALYSIS_NOT_POSSIBLE,
}

# Events stored before the trail existed: automatic jobs were attributed to the booking user.
_AUTOMATIC_COMMENT_PREFIXES = ("auto-", "auto ", "[auto", "booking cancelled automatically", "automatically ")

_scope: ContextVar[dict | None] = ContextVar("iic_booking_audit_scope", default=None)
_task_tokens: dict = {}


def _new_scope(source: str, source_label: str, *, request=None, actor=None) -> dict:
    return {"source": source, "source_label": source_label, "request": request, "actor": actor, "pending": []}


@contextmanager
def audit_scope(*, source: str, source_label: str = "", request=None, actor=None):
    """Attribute booking changes made inside the block (request, job or script) to this source / actor."""
    scope = _new_scope(source, source_label, request=request, actor=actor)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)
        _flush_pending(scope)


class BookingAuditMiddleware:
    """Web requests: changes are made by the signed-in user (DRF sets ``request.user`` once authenticated)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with audit_scope(source=SOURCE_WEB, source_label="Web portal", request=request):
            return self.get_response(request)


def _on_task_prerun(task_id=None, task=None, **kwargs) -> None:
    name = str(getattr(task, "name", "") or "")
    if name in SCHEDULED_JOB_LABELS:
        scope = _new_scope(SOURCE_SCHEDULED_JOB, f"Scheduled job: {SCHEDULED_JOB_LABELS[name]}")
    else:
        scope = _new_scope(SOURCE_BACKGROUND_JOB, f"Background job: {name}" if name else "Background job")
    _task_tokens[task_id] = (_scope.set(scope), scope)


def _on_task_postrun(task_id=None, **kwargs) -> None:
    entry = _task_tokens.pop(task_id, None)
    if entry is None:
        return
    token, scope = entry
    try:
        _scope.reset(token)
    except ValueError:
        _scope.set(None)
    _flush_pending(scope)


def connect_celery_signals() -> None:
    from celery.signals import task_postrun, task_prerun

    task_prerun.connect(_on_task_prerun, weak=False, dispatch_uid="booking_audit_task_prerun")
    task_postrun.connect(_on_task_postrun, weak=False, dispatch_uid="booking_audit_task_postrun")


def current_actor():
    """Explicit scope actor, else the signed-in request user; None for jobs, scripts and anonymous calls."""
    scope = _scope.get()
    if not scope:
        return None
    if scope.get("actor") is not None:
        return scope["actor"]
    user = getattr(scope.get("request"), "user", None)
    return user if getattr(user, "is_authenticated", False) else None


def in_background_job() -> bool:
    scope = _scope.get()
    return bool(scope and scope.get("source") in JOB_SOURCES)


def current_source() -> tuple[str, str]:
    scope = _scope.get()
    if not scope:
        return SOURCE_SYSTEM, "System process"
    return scope.get("source") or SOURCE_SYSTEM, scope.get("source_label") or "System process"


def _display_name(user) -> str:
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user) or str(getattr(user, "email", "") or "")


def actor_role_for_booking(actor, booking) -> str:
    """Role of ``actor`` on this booking: its user, the equipment's OIC / Temporary OIC / Lab Operator, an admin…"""
    from iic_booking.users.models.user_type import UserType

    from .models import EquipmentManager, EquipmentOperator, EquipmentTemporaryOIC

    if actor is None:
        return ROLE_UNKNOWN
    if getattr(actor, "pk", None) is not None and actor.pk == getattr(booking, "user_id", None):
        return ROLE_USER
    user_type = str(getattr(actor, "user_type", "") or "").strip().lower()
    if getattr(actor, "is_superuser", False) or user_type == UserType.ADMIN:
        return ROLE_MAIN_ADMIN
    if user_type == UserType.DEPT_ADMIN:
        return ROLE_DEPT_ADMIN
    equipment_id = getattr(booking, "equipment_id", None)
    if equipment_id is not None:
        if EquipmentManager.objects.filter(equipment_id=equipment_id, manager_id=actor.pk).exists():
            return ROLE_OIC
        if EquipmentTemporaryOIC.objects.active().filter(equipment_id=equipment_id, temporary_oic_id=actor.pk).exists():
            return ROLE_TEMP_OIC
        if EquipmentOperator.objects.filter(equipment_id=equipment_id, operator_id=actor.pk).exists():
            return ROLE_LAB_OPERATOR
    if user_type == UserType.MANAGER:
        return ROLE_OIC
    if user_type == UserType.OPERATOR:
        return ROLE_LAB_OPERATOR
    owner = getattr(booking, "user", None)
    if user_type == UserType.FACULTY or (owner is not None and getattr(owner, "supervisor_id", None) == actor.pk):
        return ROLE_SUPERVISOR
    if user_type in UserType.get_admin_panel_codes():
        return ROLE_OTHER_STAFF
    return ROLE_OTHER_USER


def _actor_fields(role: str, actor) -> dict:
    if role == ROLE_SYSTEM:
        kind, name = "system", "System"
    else:
        kind = "user" if role in USER_SIDE_ROLES else "staff"
        name = _display_name(actor) if actor is not None else ""
    return {
        "actor_kind": kind,
        "actor_role": role,
        "actor_role_label": ROLE_LABELS.get(role, role),
        "actor_name": name,
    }


def audit_metadata(booking, actor, *, system: bool) -> dict:
    """Who / in which role / from where, for a new booking event."""
    role = ROLE_SYSTEM if system else actor_role_for_booking(actor, booking)
    source, source_label = current_source()
    return {**_actor_fields(role, actor), "source": source, "source_label": source_label}


def reason_fields(code: str, reason: str = "", **extra) -> dict:
    """``metadata={"audit": reason_fields(...)}`` for ``create_booking_event``."""
    fields = {"reason_code": code, "reason_label": REASON_LABELS.get(code, code)}
    if reason:
        fields["reason"] = reason
    fields.update({k: v for k, v in extra.items() if v not in (None, "")})
    return fields


def disruption_kind_label(kind) -> str:
    return REASON_LABELS.get(DISRUPTION_KIND_REASONS.get(str(kind or "")), "") or "lab disruption"


def disruption_choice_audit(booking, previous_status, actor, *, choice: str) -> dict:
    """Metadata recording a refund / cancel / reschedule of a booking awaiting the user's disruption choice.

    Call before ``clear_disruption_policy_fields`` (which forgets the disruption kind).
    """
    from .models import BookingStatus

    if not (getattr(booking, "maintenance_disruption_flag", False) or previous_status == BookingStatus.DISRUPTION_PENDING):
        return {}
    kind = getattr(booking, "disruption_kind", None) or ""
    kind_label = disruption_kind_label(kind)
    by_user = actor is not None and actor.pk == getattr(booking, "user_id", None)
    who = "User" if by_user else ("System" if actor is None else "Staff")
    if choice == "reschedule":
        code = REASON_RESCHEDULE_AFTER_DISRUPTION
        text = f"User chose to reschedule after {kind_label}" if by_user else f"{who} rescheduled the booking after {kind_label}"
    elif choice == "refund":
        code = REASON_REFUND_AFTER_DISRUPTION
        text = f"User chose a full refund after {kind_label}" if by_user else f"{who} refunded the booking after {kind_label}"
    else:
        code = REASON_CANCEL_AFTER_DISRUPTION
        text = f"User cancelled after {kind_label}" if by_user else f"{who} cancelled the booking after {kind_label}"
    return {AUDIT_KEY: reason_fields(code, text, disruption_kind=kind)}


def note_status_change(booking, old_status, new_status) -> None:
    """``Booking.save`` hook: make sure the change ends up in the history (see ``_record_if_unlogged``)."""
    if not getattr(booking, "pk", None):
        return
    actor = current_actor()
    source, source_label = current_source()
    change = {
        "booking_id": booking.pk,
        "old": old_status,
        "new": new_status,
        "at": timezone.now(),
        "actor": actor,
        "source": source,
        "source_label": source_label,
    }
    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(lambda: _record_if_unlogged(change))
        return
    scope = _scope.get()
    if scope is not None:
        scope["pending"].append(change)


def _flush_pending(scope: dict | None) -> None:
    if not scope:
        return
    pending, scope["pending"] = scope.get("pending") or [], []
    for change in pending:
        _record_if_unlogged(change)


def _record_if_unlogged(change: dict) -> None:
    from .models import Booking, BookingEvent, BookingEventType, BookingStatus

    try:
        if BookingEvent.objects.filter(
            booking_id=change["booking_id"],
            new_status=change["new"],
            created_at__gte=change["at"] - timedelta(minutes=2),
        ).exists():
            return
        booking = Booking.objects.select_related("user", "equipment").filter(pk=change["booking_id"]).first()
        if booking is None:
            return
        from .booking_events import create_booking_event

        labels = dict(BookingStatus.choices)
        old_label = labels.get(change["old"], change["old"])
        new_label = labels.get(change["new"], change["new"])
        actor = change["actor"]
        create_booking_event(
            booking=booking,
            event_type=BookingEventType.STATUS_CHANGED,
            created_by=actor,
            system_actor=actor is None,
            previous_status=change["old"],
            new_status=change["new"],
            comment=f"Status changed from {old_label} to {new_label}.",
            metadata={
                AUDIT_KEY: {
                    **reason_fields(REASON_STATUS_CHANGE),
                    "auto_recorded": True,
                    "source": change["source"],
                    "source_label": change["source_label"],
                }
            },
            send_notification=False,
        )
    except Exception:
        logger.exception("Could not record status change of booking %s", change.get("booking_id"))


def _looks_automatic(comment) -> bool:
    text = str(comment or "").strip().lower()
    return text.startswith(_AUTOMATIC_COMMENT_PREFIXES)


def resolve_event_audit(event, cache: dict | None = None) -> dict:
    """Audit block for the history card; events stored before the trail are attributed from what they hold."""
    cache = cache if cache is not None else {}
    stored = (getattr(event, "metadata", None) or {}).get(AUDIT_KEY)
    stored = dict(stored) if isinstance(stored, dict) else {}
    booking = getattr(event, "booking", None)

    if not stored.get("actor_role"):
        actor = getattr(event, "created_by", None)
        system = getattr(event, "created_by_id", None) is None or _looks_automatic(getattr(event, "comment", ""))
        if system:
            role = ROLE_SYSTEM
        else:
            key = ("role", actor.pk, getattr(booking, "pk", None))
            if key not in cache:
                cache[key] = actor_role_for_booking(actor, booking)
            role = cache[key]
        stored = {**_actor_fields(role, None if system else actor), "source": "", "source_label": "", **stored}

    if not stored.get("reason_code") and booking is not None:
        reason = _cancellation_reason(event, booking, cache)
        if reason:
            stored.update(reason)
    if stored.get("reason_code") and not stored.get("reason_label"):
        stored["reason_label"] = REASON_LABELS.get(stored["reason_code"], stored["reason_code"])
    return stored


def _cancellation_reason(event, booking, cache: dict) -> dict | None:
    from .booking_cancellation_log import TRACKED_STATUSES
    from .cancellation_models import BookingCancellation

    new_status = str(getattr(event, "new_status", "") or "")
    if new_status not in TRACKED_STATUSES or getattr(event, "previous_status", None) == new_status:
        return None
    key = ("cancellation", booking.pk)
    if key not in cache:
        cache[key] = BookingCancellation.objects.filter(booking_id=booking.pk).first()
    record = cache[key]
    if record is None or record.new_status != new_status or not record.reason:
        return None
    return {"reason_code": record.reason, "reason_label": record.get_reason_display()}
