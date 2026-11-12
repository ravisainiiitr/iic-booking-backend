"""Main Administrator override of a disruption outcome on a booking.

Staff or the scheduled checks sometimes record the wrong outcome, e.g. a booking that was analysed gets marked
Booking Not Utilized. The Main Administrator can then set the right status with a reason; the booking user (and the
wallet owner) receive the usual "Booking Status Updated" email naming the old and new status and the reason.

Only outcomes that kept the charge can be overridden here (Booking Not Utilized, and a disruption still awaiting the
user's refund-or-reschedule choice); the target statuses keep the charge too, so no wallet movement is involved.
Operator Unavailable, Analysis Not Possible and Under Maintenance have already refunded the wallet and released the
slots, so changing them would mean charging again: they are refused with an explanation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .booking_events import BookingEventType, create_booking_event
from .models import (
    Booking,
    BookingDisruptionKind,
    BookingSampleTrace,
    BookingStatus,
    SampleTraceStatus,
    SlotStatus,
)

logger = logging.getLogger(__name__)

OVERRIDE_MARKER = "status_override"
REASON_MIN_LENGTH = 10

# Outcome the booking is in -> statuses the Main Administrator may set.
OVERRIDE_TARGETS: dict[str, tuple[str, ...]] = {
    BookingStatus.BOOKING_NOT_UTILIZED: (BookingStatus.COMPLETED, BookingStatus.BOOKED),
    BookingStatus.DISRUPTION_PENDING: (BookingStatus.COMPLETED, BookingStatus.BOOKING_NOT_UTILIZED),
}

REFUNDED_OUTCOMES = (
    BookingStatus.ABSENT,
    BookingStatus.OTHER_DISRUPTION,
    BookingStatus.UNDER_MAINTENANCE,
)

TARGET_HELP = {
    BookingStatus.COMPLETED: "The analysis was done. The charge stays; the booking counts as completed.",
    BookingStatus.BOOKED: "Reopen the booking so the lab can record the analysis. The charge stays.",
    BookingStatus.BOOKING_NOT_UTILIZED: "The user did not use the slot. The charge stays; no refund.",
}

# Slot status that goes with each target (slots still linked to the booking).
_SLOT_STATUS_FOR = {
    BookingStatus.COMPLETED: SlotStatus.BOOKED,
    BookingStatus.BOOKED: SlotStatus.BOOKED,
    BookingStatus.BOOKING_NOT_UTILIZED: SlotStatus.BOOKING_NOT_UTILIZED,
}


class OverrideRefused(Exception):
    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def is_main_admin(user) -> bool:
    from iic_booking.users.models import UserType

    return bool(user and user.is_authenticated and getattr(user, "user_type", None) == UserType.ADMIN)


def status_label(value: str) -> str:
    try:
        return str(BookingStatus(value).label)
    except ValueError:
        return value


def refusal_for(booking: Booking) -> str:
    """Why this booking's status cannot be overridden ("" when it can)."""
    current = booking.status
    if current in OVERRIDE_TARGETS:
        return ""
    if current in REFUNDED_OUTCOMES:
        return (
            f"This booking is {status_label(current)}: the charge was already refunded to the wallet and the slot "
            "released, so its status cannot be changed here."
        )
    return f"Only a disruption outcome can be overridden; this booking is {status_label(current)}."


def options_for(booking: Booking) -> dict:
    refusal = refusal_for(booking)
    targets = [] if refusal else OVERRIDE_TARGETS[booking.status]
    return {
        "booking_id": booking.booking_id,
        "current_status": booking.status,
        "current_status_label": status_label(booking.status),
        "allowed": not refusal,
        "refusal": refusal,
        "reason_min_length": REASON_MIN_LENGTH,
        "targets": [{"value": t, "label": status_label(t), "help": TARGET_HELP[t]} for t in targets],
    }


@dataclass
class OverrideResult:
    booking: Booking
    previous_status: str
    new_status: str
    event_id: int
    notified: bool


def override_status(booking_id: int, *, target: str, reason: str, actor, notify_user: bool = True) -> OverrideResult:
    """Set ``target`` on the booking for the Main Administrator, with a history entry and the user's email."""
    if not is_main_admin(actor):
        raise OverrideRefused("Only the Main Administrator can override a booking status.", 403)
    reason = (reason or "").strip()
    if len(reason) < REASON_MIN_LENGTH:
        raise OverrideRefused(f"Give a reason of at least {REASON_MIN_LENGTH} characters; the user sees it.")

    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update()
            .select_related("user", "equipment")
            .filter(pk=booking_id)
            .first()
        )
        if booking is None:
            raise OverrideRefused("Booking not found.", 404)
        refusal = refusal_for(booking)
        if refusal:
            raise OverrideRefused(refusal)
        if target not in OVERRIDE_TARGETS[booking.status]:
            allowed = ", ".join(status_label(t) for t in OVERRIDE_TARGETS[booking.status])
            raise OverrideRefused(f"{status_label(booking.status)} can be changed to: {allowed}.")

        previous = booking.status
        extra = {}
        if previous == BookingStatus.DISRUPTION_PENDING and booking.disruption_kind == BookingDisruptionKind.MAINTENANCE:
            # A maintenance hold decided by the Main Administrator can no longer be refunded (maintenance_settlement).
            from .maintenance_settlement import SETTLED_MARKER

            extra[SETTLED_MARKER] = True
        event = apply_status_change(
            booking,
            target=target,
            reason=reason,
            actor=actor,
            actor_label="the Main Administrator",
            notify_user=notify_user,
            metadata={OVERRIDE_MARKER: True, **extra},
        )
    logger.info(
        "booking status override booking_id=%s %s->%s by user_id=%s", booking.booking_id, previous, target, actor.pk
    )
    return OverrideResult(
        booking=booking, previous_status=previous, new_status=target, event_id=event.event_id, notified=notify_user
    )


def apply_status_change(
    booking: Booking, *, target: str, reason: str, actor, actor_label: str, notify_user: bool, metadata: dict
):
    """Set ``target`` on a booking already locked by the caller: slots, sample trace, disruption fields, history entry
    (and the user's "Booking Status Updated" email). Returns the history event."""
    previous = booking.status
    update_fields = ["status"]
    slot_status = _SLOT_STATUS_FOR[target]
    changed_slots = booking.daily_slots.exclude(status=slot_status).update(status=slot_status)

    removed_traces: list[int] = []
    if target == BookingStatus.BOOKED:
        # Not Utilized trace rows would keep the lab from recording the analysis on the reopened booking.
        removed_traces = list(
            BookingSampleTrace.objects.filter(
                booking=booking, status=SampleTraceStatus.NOT_UTILIZED
            ).values_list("id", flat=True)
        )
        BookingSampleTrace.objects.filter(id__in=removed_traces).delete()
    else:
        trace_status = (
            SampleTraceStatus.COMPLETED if target == BookingStatus.COMPLETED else SampleTraceStatus.NOT_UTILIZED
        )
        BookingSampleTrace.objects.create(
            booking=booking,
            status=trace_status,
            reason=f"Status set to {status_label(target)} by {actor_label}: {reason}",
            created_by=actor,
        )

    if previous == BookingStatus.DISRUPTION_PENDING:
        from .maintenance_policy import clear_disruption_policy_fields

        clear_disruption_policy_fields(booking, keep_quota_anchor=True)
        update_fields += [
            "maintenance_disruption_flag",
            "maintenance_decision_deadline_at",
            "maintenance_reschedule_extra_week",
            "maintenance_operational_marked_at",
            "maintenance_auto_reschedule_pending",
            "disruption_kind",
            "disruption_reason",
            "disruption_release_slot_status",
        ]
    if target == BookingStatus.COMPLETED:
        booking.completed_at = timezone.now()
        update_fields.append("completed_at")
    booking.status = target
    booking.save(update_fields=update_fields)

    return create_booking_event(
        booking=booking,
        event_type=BookingEventType.STATUS_CHANGED,
        previous_status=previous,
        new_status=target,
        comment=(
            f"Status changed from {status_label(previous)} to {status_label(target)} by {actor_label}. "
            f"Reason: {reason}"
        ),
        created_by=actor,
        send_notification=notify_user,
        metadata={
            **metadata,
            "reason": reason,
            "changed_slot_count": changed_slots,
            "removed_sample_trace_ids": removed_traces,
            "user_notified": notify_user,
        },
    )
