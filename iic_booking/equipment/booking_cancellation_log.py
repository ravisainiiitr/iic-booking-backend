"""Record who cancelled a booking, why, how late and what was refunded (``BookingCancellation``).

Every status change into a cancelled / refunded / lab-disrupted / not-utilized status goes through
``create_booking_event``, which calls ``record_from_event``; call sites pass ``cancellation`` hints for what the
event alone cannot tell (the real actor of automatic jobs, the typed notes, the refund and released slots).
Recording never raises: a failure is logged and the cancellation itself goes ahead.

The same classification rebuilds rows for older bookings from their history (``backfill_booking_cancellations``).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .cancellation_models import (
    BookingCancellation,
    CancellationActorRole,
    CancellationDataQuality,
    CancellationReason,
)

logger = logging.getLogger(__name__)

CANCELLED = "CANCELLED"
REFUNDED = "REFUNDED"
NOT_UTILIZED = "BOOKING_NOT_UTILIZED"

LAB_DISRUPTION_REASONS = {
    "UNDER_MAINTENANCE": CancellationReason.EQUIPMENT_DOWN,
    "ABSENT": CancellationReason.OPERATOR_UNAVAILABLE,
    "OTHER_DISRUPTION": CancellationReason.ANALYSIS_NOT_POSSIBLE,
}
# Statuses that end a booking without it being carried out (the charge is refunded or kept).
CANCELLATION_STATUSES = frozenset({CANCELLED, REFUNDED, *LAB_DISRUPTION_REASONS})
TRACKED_STATUSES = frozenset({*CANCELLATION_STATUSES, NOT_UTILIZED})
FULLY_REFUNDED_STATUSES = frozenset({REFUNDED, *LAB_DISRUPTION_REASONS})
UNPAID_PREVIOUS_STATUSES = frozenset({"HOLD", "WAITLISTED"})

STAFF_ROLES = frozenset(
    {
        CancellationActorRole.OIC,
        CancellationActorRole.LAB_OPERATOR,
        CancellationActorRole.DEPT_ADMIN,
        CancellationActorRole.MAIN_ADMIN,
        CancellationActorRole.OTHER_STAFF,
    }
)

LATE_MINUTES = 24 * 60

_NOTE_PREFIXES = re.compile(
    r"^(booking cancelled( and refunded)?( automatically)?( by (user|admin|system|staff))?"
    r"( \(refund requested but wallet not found\))?\.?|booking refunded\.?|"
    r"partial cancellation by \w+:[^.]*\.)\s*",
    re.IGNORECASE,
)


def actor_role_for(actor, booking, *, system: bool = False) -> str:
    if system:
        return CancellationActorRole.SYSTEM
    if actor is None:
        return CancellationActorRole.UNKNOWN
    if getattr(actor, "pk", None) is not None and actor.pk == getattr(booking, "user_id", None):
        return CancellationActorRole.USER
    user_type = str(getattr(actor, "user_type", "") or "").strip().lower()
    if getattr(actor, "is_superuser", False) or user_type == UserType.ADMIN:
        return CancellationActorRole.MAIN_ADMIN
    if user_type == UserType.DEPT_ADMIN:
        return CancellationActorRole.DEPT_ADMIN
    if user_type == UserType.MANAGER:
        return CancellationActorRole.OIC
    if user_type == UserType.OPERATOR:
        return CancellationActorRole.LAB_OPERATOR
    owner = getattr(booking, "user", None)
    if user_type == UserType.FACULTY or (owner is not None and getattr(owner, "supervisor_id", None) == actor.pk):
        return CancellationActorRole.SUPERVISOR
    return CancellationActorRole.OTHER_STAFF


def classify_reason(
    *,
    new_status: str,
    role: str,
    comment: str = "",
    event_type: str = "",
    hint: str | None = None,
) -> str:
    if hint:
        return hint
    text = (comment or "").lower()
    if "auto-cancelled" in text and ("deadline" in text or "maintenance" in text):
        return CancellationReason.DISRUPTION_DEADLINE
    if "hold released" in text:
        return CancellationReason.URGENT_HOLD_RELEASED
    if "lab rejected" in text or "not replaced within" in text:
        return CancellationReason.LAB_REJECTED_FILES
    if new_status == NOT_UTILIZED:
        return CancellationReason.NO_SHOW
    if new_status in LAB_DISRUPTION_REASONS:
        return LAB_DISRUPTION_REASONS[new_status]
    if role in (CancellationActorRole.USER, CancellationActorRole.SUPERVISOR):
        return CancellationReason.USER_REQUEST
    if role in STAFF_ROLES:
        return CancellationReason.STAFF_REFUND if event_type == REFUNDED else CancellationReason.STAFF_CANCEL
    return CancellationReason.OTHER


def clean_note(comment: str | None) -> str:
    """The typed reason without the generated "Booking cancelled by user." prefix."""
    text = (comment or "").strip()
    previous = None
    while text and text != previous:
        previous = text
        text = _NOTE_PREFIXES.sub("", text, count=1).strip()
    return text[:2000]


def _money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def charged_amount(booking, previous_status: str) -> Decimal:
    """What the user had paid for the booking when it was cancelled."""
    if previous_status in UNPAID_PREVIOUS_STATUSES:
        return Decimal("0.00")
    if previous_status == "PENDING_PAYMENT":
        return _money(getattr(booking, "wallet_amount_applied", 0)) or Decimal("0.00")
    return _money(getattr(booking, "total_charge", 0)) or Decimal("0.00")


def refund_for(new_status: str, charge: Decimal, recorded) -> tuple[Decimal | None, bool]:
    """``(refund, estimated)``: the recorded amount, else none for plain cancellations and no-shows, else the charge."""
    amount = _money(recorded)
    if amount is not None:
        return amount, False
    if new_status in FULLY_REFUNDED_STATUSES:
        return charge, True
    return Decimal("0.00"), False


def slot_range_for(booking) -> tuple[Any, Any]:
    from .models import BookingSlotRange, DailySlot

    row = DailySlot.objects.filter(booking_id=booking.pk).aggregate(start=Min("start_datetime"), end=Max("end_datetime"))
    if row["start"]:
        return row["start"], row["end"]
    released = BookingSlotRange.objects.filter(booking_id=booking.pk).values("start_datetime", "end_datetime").first()
    if released:
        return released["start_datetime"], released["end_datetime"]
    return None, None


def lead_minutes(slot_start, cancelled_at) -> int | None:
    if not slot_start or not cancelled_at:
        return None
    return int((slot_start - cancelled_at).total_seconds() // 60)


def _event_actor_and_system(event, hints: dict) -> tuple[Any, bool]:
    if "actor" in hints:
        actor = hints.get("actor")
        return actor, bool(hints.get("system")) or actor is None
    if hints.get("system"):
        return None, True
    return getattr(event, "created_by", None), getattr(event, "created_by_id", None) is None


def record_cancellation(
    booking,
    *,
    previous_status: str,
    new_status: str,
    actor=None,
    system: bool = False,
    reason: str | None = None,
    note: str = "",
    comment: str = "",
    event_type: str = "",
    refund_amount=None,
    released_slot_ids=None,
    cancelled_at=None,
    event=None,
    data_quality: str = CancellationDataQuality.RECORDED,
) -> BookingCancellation | None:
    """Create or refresh the booking's cancellation row (a later refund of a cancelled booking updates it)."""
    cancelled_at = cancelled_at or timezone.now()
    existing = BookingCancellation.objects.filter(booking_id=booking.pk).first()
    if existing is not None and previous_status in TRACKED_STATUSES:
        recorded = _money(refund_amount)
        existing.new_status = new_status
        if recorded is not None:
            existing.refund_amount = (existing.refund_amount or Decimal("0.00")) + recorded
            existing.refund_estimated = False
        elif new_status in FULLY_REFUNDED_STATUSES and not existing.refund_amount:
            existing.refund_amount, existing.refund_estimated = existing.charge_amount, True
        if note and not existing.note:
            existing.note = note
        existing.save(update_fields=["new_status", "refund_amount", "refund_estimated", "note", "updated_at"])
        return existing

    role = actor_role_for(actor, booking, system=system)
    charge = charged_amount(booking, previous_status)
    refund, estimated = refund_for(new_status, charge, refund_amount)
    start, end = slot_range_for(booking)
    values = {
        "cancelled_at": cancelled_at,
        "cancelled_by": actor if getattr(actor, "pk", None) else None,
        "actor_role": role,
        "reason": classify_reason(
            new_status=new_status, role=role, comment=comment or note, event_type=event_type, hint=reason
        ),
        "note": (note or clean_note(comment))[:2000],
        "previous_status": previous_status or "",
        "new_status": new_status,
        "charge_amount": charge,
        "refund_amount": refund,
        "refund_estimated": estimated,
        "slot_start": start,
        "slot_end": end,
        "lead_minutes": lead_minutes(start, cancelled_at),
        "released_slot_ids": [int(x) for x in (released_slot_ids or []) if str(x).isdigit()],
        "data_quality": data_quality,
        "event": event,
    }
    row, _ = BookingCancellation.objects.update_or_create(booking_id=booking.pk, defaults=values)
    return row


_AUTOMATIC_MARKERS = ("cancelled automatically", "auto-cancelled", "automatically cancelled")


def history_actor(event, booking) -> tuple[Any, bool]:
    """``(actor, system)`` for an event recorded before cancellations were logged.

    Automatic jobs used to store the booking user as the event's author, so their text decides; urgent holds were
    released by an OIC or by expiry, which the old events cannot tell apart (left as not recorded).
    """
    text = str(getattr(event, "comment", "") or "").lower()
    if any(marker in text for marker in _AUTOMATIC_MARKERS):
        return None, True
    metadata = getattr(event, "metadata", None) or {}
    actor = getattr(event, "created_by", None)
    if metadata.get("urgent_hold_released") or "hold released" in text:
        if actor is None or actor.pk == getattr(booking, "user_id", None):
            return None, False
        return actor, False
    return actor, actor is None


def replay_history(booking, events, *, data_quality: str = CancellationDataQuality.FROM_HISTORY) -> bool:
    """Rebuild the booking's row from its status events (oldest first); ``True`` when a row was written."""
    wrote = False
    for event in events:
        new_status = str(event.new_status or "")
        previous_status = str(event.previous_status or "")
        if new_status in TRACKED_STATUSES and previous_status != new_status:
            actor, system = history_actor(event, booking)
            metadata = event.metadata or {}
            record_cancellation(
                booking,
                previous_status=previous_status,
                new_status=new_status,
                actor=actor,
                system=system,
                comment=str(event.comment or ""),
                event_type=str(event.event_type or ""),
                refund_amount=metadata.get("refund_amount"),
                cancelled_at=event.created_at,
                event=event,
                data_quality=data_quality,
            )
            wrote = True
        elif previous_status in TRACKED_STATUSES and new_status and new_status not in TRACKED_STATUSES:
            BookingCancellation.objects.filter(booking_id=booking.pk).delete()
            wrote = False
    return wrote


def released_slots_in_range(booking, start, end) -> list[int]:
    """Slots of the equipment inside the booking's remembered range (the slots it most likely released)."""
    from .models import DailySlot

    if not start or not end:
        return []
    return list(
        DailySlot.objects.filter(
            slot_master__equipment_id=booking.equipment_id,
            start_datetime__gte=start,
            end_datetime__lte=end,
        )
        .order_by("start_datetime")
        .values_list("id", flat=True)[:500]
    )


def record_cancellation_safely(booking, **kwargs) -> None:
    """``record_cancellation`` for paths that change the status without a booking event; never raises."""
    try:
        with transaction.atomic():
            record_cancellation(booking, **kwargs)
    except Exception:
        logger.exception("Could not record the cancellation of booking %s", getattr(booking, "pk", None))


def record_from_event(event, hints: dict | None = None) -> None:
    """Hook for ``create_booking_event``: record (or drop, when a booking is restored) the cancellation row."""
    new_status = str(getattr(event, "new_status", "") or "")
    previous_status = str(getattr(event, "previous_status", "") or "")
    entering = new_status in TRACKED_STATUSES and (previous_status != new_status)
    leaving = previous_status in TRACKED_STATUSES and new_status and new_status not in TRACKED_STATUSES
    if not entering and not leaving:
        return
    hints = dict(hints or {})
    try:
        with transaction.atomic():
            if leaving:
                BookingCancellation.objects.filter(booking_id=event.booking_id).delete()
                return
            actor, system = _event_actor_and_system(event, hints)
            metadata = getattr(event, "metadata", None) or {}
            refund = hints.get("refund_amount", metadata.get("refund_amount"))
            record_cancellation(
                event.booking,
                previous_status=previous_status,
                new_status=new_status,
                actor=actor,
                system=system,
                reason=hints.get("reason"),
                note=str(hints.get("note") or "").strip(),
                comment=str(getattr(event, "comment", "") or ""),
                event_type=str(getattr(event, "event_type", "") or ""),
                refund_amount=refund,
                released_slot_ids=hints.get("released_slot_ids"),
                cancelled_at=getattr(event, "created_at", None),
                event=event,
            )
    except Exception:
        logger.exception("Could not record the cancellation of booking %s", getattr(event, "booking_id", None))
