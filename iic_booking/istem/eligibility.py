"""Which local bookings may be sent to I-STEM. Database reads only."""

from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import conf

ELIGIBLE_STATUSES = ("BOOKED", "PROCESSING", "COMPLETED")
# Manual FBR workflow states that mean the user already submitted an FBR from their own I-STEM booking
# (``equipment.IstemFbrStatus``): PENDING_OIC = awaiting OIC, INVALID = rejected for correction, EXECUTED = verified.
MANUAL_FBR_SUBMITTED_STATUSES = ("PENDING_OIC", "INVALID", "EXECUTED")


def has_manual_fbr(booking) -> bool:
    """True when the booking is in the manual FBR flow past "not submitted": creating it on I-STEM would duplicate the
    I-STEM booking the user made themselves."""
    return bool((booking.istem_fbr_number or "").strip()) or booking.istem_fbr_status in MANUAL_FBR_SUBMITTED_STATUSES


def sync_start_at() -> datetime | None:
    """Optional cut-over: bookings created earlier are never sent (no backfill)."""
    raw = str(getattr(settings, "ISTEM_SYNC_START_AT", "") or "").strip()
    if not raw:
        return None
    value = parse_datetime(raw)
    if value is not None and timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def evaluate(booking) -> tuple[bool, str]:
    """(eligible, reason). Eligible = the booking has reached its confirmed, paid state and needs an I-STEM FBR.

    Local lifecycle: a booking becomes BOOKED once its slots are held; external users with an amount due stay in
    PENDING_PAYMENT until settlement moves them to BOOKED. BOOKED bookings, and those that moved on to PROCESSING /
    COMPLETED before the sync ran, are sent once fully settled; unpaid, held, waitlisted, cancelled or refunded
    bookings never reach I-STEM.
    """
    from iic_booking.equipment.booking_payment_service import booking_payment_fully_settled

    if conf.istem_mode() == conf.MODE_OFF:
        return False, "integration off"
    profile = getattr(booking, "charge_profile", None)
    if not getattr(profile, "require_istem_fbr", False):
        return False, "charge profile does not require an I-STEM FBR"
    if booking.status not in ELIGIBLE_STATUSES:
        return False, f"booking status is {booking.status}"
    if has_manual_fbr(booking):
        return False, "manual FBR already submitted"
    if not booking_payment_fully_settled(booking):
        return False, "payment not settled"
    start = sync_start_at()
    if start is not None and booking.created_at < start:
        return False, "created before ISTEM_SYNC_START_AT"
    return True, ""


def evaluate_execute(booking, link) -> tuple[bool, str]:
    """(eligible, reason) for FBR Execute: the local booking is genuinely completed and paid, and the I-STEM booking
    was created and confirmed (request_id and confirmation_id known)."""
    from iic_booking.equipment.booking_payment_service import booking_payment_fully_settled

    if conf.istem_mode() == conf.MODE_OFF:
        return False, "integration off"
    if not conf.execute_enabled():
        return False, "FBR Execute disabled"
    if booking.status != "COMPLETED" or not booking.completed_at:
        return False, f"booking status is {booking.status}"
    if not booking_payment_fully_settled(booking):
        return False, "payment not settled"
    if link is None or not link.request_id:
        return False, "no I-STEM request_id"
    if not link.confirmation_id:
        return False, "no confirmation_id"
    return True, ""
