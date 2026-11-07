"""Portal events that also go out on WhatsApp, for users who opted in, while WhatsApp is on. Nothing here ever raises.

Every function stops at once while WhatsApp is off, so with sending disabled these add one settings check and no query.
service.notify enforces opt-in, the pilot list and approved templates, and its event key stops duplicates.
"""

from __future__ import annotations

import logging
from functools import wraps

from django.db import transaction
from django.utils import timezone

from . import conf

logger = logging.getLogger(__name__)

AS_BOOKED = "as booked"


def _guarded(func):
    """A WhatsApp problem must never affect a booking or a payment. The savepoint protects the caller's transaction."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not conf.enabled():
            return None
        try:
            with transaction.atomic():
                return func(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("whatsapp hook %s failed", func.__name__)
            return None

    return wrapper


def _notify(user, event, params, key):
    from .service import notify

    return notify(user, event, params, event_key=key)


def _person(user) -> str:
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user) or "User"


def _ref(booking) -> str:
    return booking.virtual_booking_id or str(booking.pk)


def _equipment(booking_or_equipment) -> str:
    equipment = getattr(booking_or_equipment, "equipment", booking_or_equipment)
    return getattr(equipment, "name", "") or ""


def _first_slot(booking):
    slots = list(booking.daily_slots.order_by("start_datetime")[:1]) if hasattr(booking, "daily_slots") else []
    return slots[0].start_datetime if slots and slots[0].start_datetime else None


def _date(moment) -> str:
    return timezone.localtime(moment).strftime("%d %b %Y") if moment else AS_BOOKED


def _time(moment) -> str:
    return timezone.localtime(moment).strftime("%I:%M %p") if moment else AS_BOOKED


def _money(amount) -> str:
    return f"Rs. {float(amount or 0):,.2f}"


def _base(booking) -> list[str]:
    return [_person(booking.user), _ref(booking), _equipment(booking)]


def _with_slot_date(booking) -> list[str]:
    return [*_base(booking), _date(_first_slot(booking))]


def _with_slot_time(booking) -> list[str]:
    start = _first_slot(booking)
    return [*_base(booking), _date(start), _time(start)]


@_guarded
def booking_event(event) -> None:
    """Called after a booking event's email/push are queued."""
    from iic_booking.equipment.models import BookingEventType, BookingStatus

    booking = event.booking
    kind = event.event_type
    chosen = None
    if kind == BookingEventType.CREATED:
        if booking.status == BookingStatus.PENDING_PAYMENT:
            chosen = ("payment_pending", [*_base(booking), _money(booking.amount_due)])
        elif booking.status == BookingStatus.BOOKED:
            chosen = ("booking_confirmed", _with_slot_time(booking))
    elif kind == BookingEventType.CONFIRMED or (
        kind == BookingEventType.STATUS_CHANGED
        and getattr(event, "new_status", "") == BookingStatus.BOOKED
        and getattr(event, "previous_status", "") in (BookingStatus.PENDING, BookingStatus.PENDING_PAYMENT, BookingStatus.WAITLISTED)
    ):
        chosen = ("booking_confirmed", _with_slot_time(booking))
    elif kind in (BookingEventType.CANCELLED, BookingEventType.REFUNDED):
        chosen = ("booking_cancelled", _with_slot_date(booking))
    elif kind == BookingEventType.RESCHEDULED:
        chosen = ("reschedule_approved", _with_slot_time(booking))
    elif kind == BookingEventType.COMPLETED:
        chosen = ("booking_completed", [*_base(booking), _date(booking.completed_at or timezone.now())])
    if chosen is not None:
        _notify(booking.user, chosen[0], chosen[1], f"booking:{booking.pk}:{chosen[0]}:{event.event_id}")


@_guarded
def booking_rejected(booking) -> None:
    """The laboratory rejected a fabrication job. Each rejection has its own time, so a second one is a new message."""
    stamp = booking.fabrication_rejected_at.isoformat() if booking.fabrication_rejected_at else ""
    _notify(booking.user, "booking_rejected", _with_slot_date(booking), f"booking:{booking.pk}:rejected:{stamp}")


@_guarded
def booking_reminder(booking) -> None:
    start = _first_slot(booking)
    _notify(booking.user, "booking_reminder", _with_slot_time(booking), f"booking:{booking.pk}:reminder:{_date(start)}")


@_guarded
def waitlist_slot_available(entry, equipment, slot_start) -> None:
    _notify(entry.user, "waitlist_slot_available", [_person(entry.user), _equipment(equipment), _date(slot_start), _time(slot_start)],
            f"waitlist:{entry.pk}:{slot_start.isoformat()}")


@_guarded
def payment_order_saved(order, created: bool) -> None:
    """The outcome of an online payment for a booking. Opening checkout sends nothing: many are abandoned, and the user is
    there on the page. Wallet recharges and external checkouts with no booking yet are not covered."""
    from iic_booking.payments.models import PaymentOrderStatus

    if not order.booking_id:
        return
    if order.status == PaymentOrderStatus.PAID:
        event = "payment_successful"
    elif order.status == PaymentOrderStatus.FAILED:
        event = "payment_failed"
    else:
        return
    booking = order.booking
    _notify(order.user, event, [_person(order.user), _ref(booking), _equipment(booking), _money(order.total_amount)],
            f"payment:{order.pk}:{event}")


@_guarded
def payment_refund_saved(refund, created: bool) -> None:
    from iic_booking.payments.models import RefundStatus

    order = refund.payment.payment_order
    if not order.booking_id:
        return
    if refund.status == RefundStatus.PROCESSED:
        event = "refund_completed"
    elif created and refund.status == RefundStatus.PENDING:
        event = "refund_initiated"
    else:
        return
    _notify(order.user, event, [_person(order.user), _ref(order.booking), _money(refund.amount)], f"gateway-refund:{refund.pk}:{event}")


@_guarded
def external_refund_saved(refund, created: bool) -> None:
    from iic_booking.external_services.models import RefundStatus

    if refund.status == RefundStatus.CLOSED:
        event = "refund_completed"
    elif created and refund.status == RefundStatus.QUEUED:
        event = "refund_initiated"
    else:
        return
    _notify(refund.user, event, [_person(refund.user), _ref(refund.booking), _money(refund.refund_amount)], f"refund:{refund.pk}:{event}")


@_guarded
def sample_trace_saved(trace, created: bool) -> None:
    from iic_booking.equipment.models import SampleTraceStatus

    if not created or trace.status != SampleTraceStatus.SAMPLE_ACCEPTED:
        return
    booking = trace.booking
    _notify(booking.user, "sample_received", [*_base(booking), _date(trace.created_at or timezone.now())], f"sample:{trace.pk}")
