"""Settle bookings on hold for an equipment maintenance before the equipment is marked Operational again.

While equipment is Under Maintenance its affected bookings wait in "Awaiting your choice (disruption)" (kind
Maintenance): the user may cancel for a full refund, or wait to be rescheduled automatically once the equipment works
again (``maintenance_reschedule``). Sometimes the lab already served the booking (or the user never came), so the
Officer In-Charge of that equipment, or the Main Administrator, closes it with a final status first:

* Completed - the analysis was done; the charge stays.
* Booking Not Utilized - the slot was not used; the charge stays.

The change goes through the same engine as the Main Administrator's status override (slots, sample trace, history
entry, "Booking Status Updated" email). The history entry carries ``SETTLED_MARKER``; from then on the booking can no
longer be refunded by any path (``refund_lock_reason``), even if the user had chosen to wait. Bookings whose user
already took the refund are no longer on hold, so they are never listed or touched.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from .booking_status_override import (
    REASON_MIN_LENGTH,
    OverrideRefused,
    OverrideResult,
    apply_status_change,
    status_label,
)
from .models import Booking, BookingDisruptionKind, BookingEvent, BookingStatus

logger = logging.getLogger(__name__)

SETTLED_MARKER = "maintenance_settled"

SETTLE_TARGETS: tuple[str, ...] = (BookingStatus.COMPLETED, BookingStatus.BOOKING_NOT_UTILIZED)

TARGET_HELP = {
    BookingStatus.COMPLETED: "The analysis was done. The charge stays and the booking can no longer be refunded.",
    BookingStatus.BOOKING_NOT_UTILIZED: (
        "The slot was not used. The charge stays and the booking can no longer be refunded."
    ),
}

REFUND_LOCKED_MESSAGE = (
    "This booking was settled by the Officer In-Charge or the Main Administrator during the equipment maintenance, "
    "so it can no longer be refunded."
)


def _is_main_admin(user) -> bool:
    from iic_booking.users.models import UserType

    return bool(user and user.is_authenticated and getattr(user, "user_type", None) == UserType.ADMIN)


def _is_oic_of(user, equipment_id) -> bool:
    from iic_booking.users.models import UserType

    from .reports import get_equipment_ids_managed_by_oic

    if not (user and user.is_authenticated and getattr(user, "user_type", None) == UserType.MANAGER):
        return False
    return int(equipment_id) in {int(i) for i in get_equipment_ids_managed_by_oic(user.pk)}


def may_settle(user, equipment_id) -> bool:
    """Main Administrator, or the Officer In-Charge (or active temporary OIC) of this equipment. Nobody else."""
    return _is_main_admin(user) or _is_oic_of(user, equipment_id)


def actor_label(user) -> str:
    return "the Main Administrator" if _is_main_admin(user) else "the Officer In-Charge"


def settleable_bookings(equipment_id):
    """Bookings of this equipment on hold for maintenance whose user has not taken a refund."""
    return Booking.objects.filter(
        equipment_id=equipment_id,
        status=BookingStatus.DISRUPTION_PENDING,
        disruption_kind=BookingDisruptionKind.MAINTENANCE,
    )


def _iso(dt):
    return timezone.localtime(dt).isoformat(timespec="minutes") if dt else None


def overview(equipment) -> dict:
    from iic_booking.communication.utils import booking_display_id_for_email

    from .maintenance_policy import is_equipment_under_maintenance_status

    rows = []
    qs = (
        settleable_bookings(equipment.pk)
        .select_related("user")
        .prefetch_related("daily_slots")
        .order_by("created_at", "booking_id")
    )
    for b in qs:
        slots = sorted((s for s in b.daily_slots.all() if s.start_datetime), key=lambda s: s.start_datetime)
        user = b.user
        rows.append(
            {
                "booking_id": b.booking_id,
                "booking_ref": booking_display_id_for_email(b),
                "user_name": (user.get_full_name() or "").strip() or user.email,
                "user_email": user.email,
                "start": _iso(slots[0].start_datetime) if slots else None,
                "end": _iso(slots[-1].end_datetime) if slots else None,
                "total_charge": str(b.total_charge),
                "waiting_for_reschedule": bool(b.maintenance_auto_reschedule_pending),
            }
        )
    return {
        "equipment_id": equipment.pk,
        "equipment_name": equipment.name,
        "equipment_status": equipment.status,
        "under_maintenance": is_equipment_under_maintenance_status(equipment.status),
        "reason_min_length": REASON_MIN_LENGTH,
        "targets": [{"value": t, "label": status_label(t), "help": TARGET_HELP[t]} for t in SETTLE_TARGETS],
        "bookings": rows,
    }


def settle_booking(booking_id: int, *, target: str, reason: str, actor, notify_user: bool = True) -> OverrideResult:
    reason = " ".join((reason or "").split())
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update()
            .select_related("user", "equipment")
            .filter(pk=booking_id)
            .first()
        )
        if booking is None:
            raise OverrideRefused("Booking not found.", 404)
        if not may_settle(actor, booking.equipment_id):
            raise OverrideRefused(
                "Only the Officer In-Charge of this equipment or the Main Administrator can settle this booking.", 403
            )
        if booking.status != BookingStatus.DISRUPTION_PENDING or booking.disruption_kind != BookingDisruptionKind.MAINTENANCE:
            raise OverrideRefused(
                f"Only a booking on hold for equipment maintenance can be settled; this booking is "
                f"{status_label(booking.status)}."
            )
        if target not in SETTLE_TARGETS:
            allowed = ", ".join(status_label(t) for t in SETTLE_TARGETS)
            raise OverrideRefused(f"A booking on hold for maintenance can be settled as: {allowed}.")
        if len(reason) < REASON_MIN_LENGTH:
            raise OverrideRefused(f"Give a reason of at least {REASON_MIN_LENGTH} characters; the user sees it.")
        previous = booking.status
        event = apply_status_change(
            booking,
            target=target,
            reason=reason,
            actor=actor,
            actor_label=actor_label(actor),
            notify_user=notify_user,
            metadata={SETTLED_MARKER: True, "settled_by_role": getattr(actor, "user_type", "")},
        )
    logger.info(
        "maintenance settlement booking_id=%s %s->%s by user_id=%s", booking.booking_id, previous, target, actor.pk
    )
    return OverrideResult(
        booking=booking, previous_status=previous, new_status=target, event_id=event.event_id, notified=notify_user
    )


def is_refund_locked(booking) -> bool:
    """True once the OIC or Main Administrator settled this booking during a maintenance hold."""
    booking_pk = getattr(booking, "pk", booking)
    if booking_pk is None:
        return False
    return BookingEvent.objects.filter(booking_id=booking_pk, **{f"metadata__{SETTLED_MARKER}": True}).exists()


def refund_lock_reason(booking) -> str:
    """Message refusing a refund of a settled booking ("" when the booking may be refunded)."""
    return REFUND_LOCKED_MESSAGE if is_refund_locked(booking) else ""
