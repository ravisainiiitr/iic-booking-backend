"""Shared logic for marking bookings as Booking Not Utilized (no refund)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import booking_display_id_for_email

from .booking_events import BookingEventType, create_booking_event
from .models import (
    Booking,
    BookingSampleTrace,
    BookingStatus,
    SampleTraceStatus,
    SlotStatus,
)
from .sample_trace_policy import trace_allows_booking_not_utilized

User = get_user_model()
logger = logging.getLogger(__name__)


def _normalize_daily_slot_end_datetime(end_dt):
    """
    Make slot end comparable to timezone.now().

    Eligibility uses **DailySlot.end_datetime** only (not date-only fields). Naive values are
    interpreted in the current Django timezone.
    """
    if end_dt is None:
        return None
    if timezone.is_naive(end_dt):
        return timezone.make_aware(end_dt, timezone.get_current_timezone())
    return end_dt


def latest_booked_slot_end_datetime(booking: Booking):
    """
    Latest ``DailySlot.end_datetime`` among slots still in BOOKED status for this booking.

    Used for the minimum 24h rule: automation runs only when
    ``timezone.now() >= latest_end + timedelta(hours=24)``.
    """
    end_times = []
    for s in booking.daily_slots.filter(status=SlotStatus.BOOKED):
        ed = _normalize_daily_slot_end_datetime(getattr(s, "end_datetime", None))
        if ed is not None:
            end_times.append(ed)
    if not end_times:
        return None
    return max(end_times)


def send_booking_not_utilized_emails(
    booking: Booking,
    booked_slots: list,
    *,
    created_by: Optional[User] = None,
) -> None:
    """Notify booking user and faculty wallet owner (same templates as manual staff action)."""
    equipment = booking.equipment
    equipment_name = (
        (getattr(equipment, "name", None) or getattr(equipment, "code", None) or "Equipment")
        if equipment
        else "Equipment"
    )
    user = booking.user
    slot_parts = []
    for s in booked_slots:
        part = str(s.date)
        if getattr(s, "start_datetime", None) and getattr(s, "end_datetime", None):
            part += f" {s.start_datetime.strftime('%H:%M')}-{s.end_datetime.strftime('%H:%M')}"
        slot_parts.append(part)
    slot_details = "; ".join(slot_parts)
    ctx = {
        "user_name": getattr(user, "name", None) or getattr(user, "email", None) or "User",
        "user_email": getattr(user, "email", "") or "",
        "equipment_name": equipment_name,
        "slot_details": slot_details,
        "booking_id": booking_display_id_for_email(booking),
    }
    try:
        CommunicationService.send_email(
            recipient=user,
            template="booking_not_utilized_email",
            template_context=ctx,
            created_by=created_by,
        )
    except Exception as e:
        logger.warning(
            "Failed to send booking not utilized email to %s: %s", getattr(user, "email", ""), e
        )

    try:
        wallet = getattr(user, "get_accessible_wallet", None) and user.get_accessible_wallet()
        if wallet and getattr(wallet, "user_id", None) != user.id and getattr(wallet, "user", None):
            owner = wallet.user
            owner_ctx = {
                **ctx,
                "student_name": getattr(user, "name", None) or getattr(user, "email", None) or "Student",
                "student_email": getattr(user, "email", "") or "",
                "wallet_owner_name": getattr(owner, "name", None) or getattr(owner, "email", None) or "Faculty",
            }
            CommunicationService.send_email(
                recipient=owner,
                template="booking_not_utilized_wallet_owner_email",
                template_context=owner_ctx,
                created_by=created_by,
            )
    except Exception as e:
        logger.warning("Failed to send booking not utilized email to wallet owner: %s", e)


def apply_booking_not_utilized(
    booking: Booking,
    *,
    actor: Optional[User],
    automated: bool = False,
    hours_after_last_slot_end: int = 0,
) -> bool:
    """
    Set booking and its BOOKED slots to Booking Not Utilized, add trace row, log event, send emails.
    No refund.

    When ``automated`` is True and ``actor`` is None, the booking event is stored with null created_by.

    The time gate uses only **end_datetime** on **BOOKED** slots: for ``hours_after_last_slot_end=N``,
    requires ``now >= max(end_datetime) + N hours`` (see ``latest_booked_slot_end_datetime``).

    Returns True if the booking was updated, False if current state does not allow it (idempotent/race).
    """
    trace_reason = (
        "Automatically marked as Booking Not Utilized: latest booked slot end_datetime was over 24 hours ago and sample "
        "lifecycle had no update or only Sample Sent. No refund issued."
        if automated
        else "Booking marked as Not Utilized by staff. No refund issued."
    )
    event_comment = (
        "Automatically marked as Booking Not Utilized (scheduled check). No refund issued."
        if automated
        else "Booking marked as Not Utilized. No refund issued."
    )

    with transaction.atomic():
        locked = (
            Booking.objects.select_for_update()
            .select_related("user", "equipment")
            .filter(pk=booking.pk)
            .first()
        )
        if not locked or locked.status != BookingStatus.BOOKED:
            return False

        if not trace_allows_booking_not_utilized(locked.booking_id):
            return False

        booked_slots = list(
            locked.daily_slots.filter(status=SlotStatus.BOOKED).select_related(
                "booking", "booking__user", "booking__equipment"
            )
        )
        if not booked_slots:
            return False
        if locked.daily_slots.exclude(status=SlotStatus.BOOKED).exists():
            return False

        latest_end = latest_booked_slot_end_datetime(locked)
        if latest_end is None:
            return False
        deadline = latest_end + timedelta(hours=hours_after_last_slot_end)
        if timezone.now() < deadline:
            return False

        locked.daily_slots.filter(status=SlotStatus.BOOKED).update(status=SlotStatus.BOOKING_NOT_UTILIZED)
        BookingSampleTrace.objects.create(
            booking=locked,
            status=SampleTraceStatus.NOT_UTILIZED,
            reason=trace_reason,
            created_by=actor,
        )

        previous_status = locked.status
        locked.status = BookingStatus.BOOKING_NOT_UTILIZED
        locked.save(update_fields=["status"])

        create_booking_event(
            booking=locked,
            event_type=BookingEventType.STATUS_CHANGED,
            previous_status=previous_status,
            new_status=BookingStatus.BOOKING_NOT_UTILIZED,
            comment=event_comment,
            created_by=actor,
            system_actor=bool(automated and actor is None),
            send_notification=False,
        )

    send_booking_not_utilized_emails(locked, booked_slots, created_by=actor)
    return True
