"""Booking events that also go out on WhatsApp (for users who opted in, while WhatsApp is on)."""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from . import conf

logger = logging.getLogger(__name__)


def _slot_text(booking) -> str:
    slots = list(booking.daily_slots.order_by("start_datetime")[:1]) if hasattr(booking, "daily_slots") else []
    if not slots or not slots[0].start_datetime:
        return "as booked"
    return timezone.localtime(slots[0].start_datetime).strftime("%a %d %b %Y, %I:%M %p")


def booking_event(event) -> None:
    """Called after a booking event's email/push are queued. Never raises."""
    try:
        from iic_booking.equipment.models import BookingEventType

        from .service import notify

        if not conf.enabled():
            return
        booking = event.booking
        ref = booking.virtual_booking_id or str(booking.pk)
        name = getattr(booking.equipment, "name", "") or ""
        mapping = {
            BookingEventType.CREATED: ("booking_confirmed", [ref, name, _slot_text(booking)]),
            BookingEventType.CANCELLED: ("booking_cancelled", [ref, name, _slot_text(booking)]),
            BookingEventType.REFUNDED: ("booking_cancelled", [ref, name, _slot_text(booking)]),
            BookingEventType.COMPLETED: ("booking_completed", [ref, name, f"{conf.portal_url()}/my-bookings"]),
        }
        chosen = mapping.get(event.event_type)
        if chosen is None:
            return
        # A savepoint: a database error here must not break a transaction the booking code is still using.
        with transaction.atomic():
            notify(booking.user, chosen[0], chosen[1], event_key=f"booking:{booking.pk}:{chosen[0]}:{event.event_id}")
    except Exception:  # noqa: BLE001 - a WhatsApp problem must never affect the booking
        logger.exception("whatsapp hook failed for booking event %s", getattr(event, "pk", None))
