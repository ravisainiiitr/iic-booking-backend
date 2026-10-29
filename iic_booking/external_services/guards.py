"""Server-side guards called from the existing booking entry points (kept small so those views change little)."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.utils import timezone

from .window import ExternalWindowClosed, check_booking_dates, is_external_booking_user


def _dates(slots):
    return [getattr(s, "date", None) for s in slots if getattr(s, "date", None)]


def ordinary_booking_guard(booking_user, slots, *, exempt: bool) -> None:
    """Refuse an ordinary booking or reschedule by an external user onto dates outside the central external window.

    Raised as ``QuotaExceededError`` so the booking view's existing handler returns the structured payload
    (``code: EXTERNAL_WINDOW_CLOSED``) and logs the failed attempt. No effect while the window is disabled, for
    internal users, or when ``exempt`` (Main Administrator / OIC booking or rescheduling for a user).
    """
    from iic_booking.equipment.quota_breakdown import QuotaExceededError

    try:
        check_booking_dates(booking_user, _dates(slots), exempt=exempt)
    except ExternalWindowClosed as exc:
        fields = exc.payload()
        message = fields.pop("error")
        raise QuotaExceededError(message, fields) from exc


def staff_exempt(actor, equipment, booking_user) -> bool:
    """Same exemption as ordinary booking: admin-panel users, and staff who may skip period limits for this user."""
    from iic_booking.equipment.api_views import staff_booking_skips_period_limits
    from iic_booking.users.models.user_type import UserType

    if getattr(actor, "user_type", None) in UserType.get_admin_panel_codes():
        return True
    return bool(staff_booking_skips_period_limits(actor, equipment, booking_user))


def reschedule_window_refusal(actor, equipment, booking_user, dates):
    """Error payload when an external user's booking would move onto dates outside the window, else None."""
    try:
        check_booking_dates(booking_user, list(dates), exempt=staff_exempt(actor, equipment, booking_user))
    except ExternalWindowClosed as exc:
        return exc.payload()
    return None


# External users reschedule a week's slots until Wednesday 20:30 of the week before, so the slots they release are open
# when internal booking for that week opens at 21:00 on Wednesday. Later, only the Officer in Charge or the Main
# Administrator reschedules (the user can message the lab from the booking details page).
EXTERNAL_RESCHEDULE_CUTOFF_WEEKDAY = 2  # Wednesday
EXTERNAL_RESCHEDULE_CUTOFF_TIME = time(20, 30)


def external_reschedule_cutoff(first_slot_start) -> datetime:
    """Wednesday 20:30 (local time) of the week before the week of ``first_slot_start``."""
    local = timezone.localtime(first_slot_start)
    monday = local.date() - timedelta(days=local.weekday())
    cutoff_day = monday - timedelta(days=7) + timedelta(days=EXTERNAL_RESCHEDULE_CUTOFF_WEEKDAY)
    return timezone.make_aware(datetime.combine(cutoff_day, EXTERNAL_RESCHEDULE_CUTOFF_TIME))


def external_reschedule_refusal(booking_user, first_slot_start, *, now=None):
    """Error payload when an external user reschedules their own booking after the weekly cut-off, else None."""
    if first_slot_start is None or not is_external_booking_user(booking_user):
        return None
    cutoff = external_reschedule_cutoff(first_slot_start)
    if (now or timezone.now()) <= cutoff:
        return None
    return {
        "error": (
            f"Slots of this week could be rescheduled until {timezone.localtime(cutoff):%A %d %b %Y, %H:%M}, so that "
            "the released slots open to internal users at 21:00 that Wednesday. Please send a message to the lab "
            "from the booking details page; the Officer in Charge or the Main Administrator can reschedule it."
        ),
        "code": "EXTERNAL_RESCHEDULE_CUTOFF",
        "cutoff": timezone.localtime(cutoff).isoformat(),
    }


def external_free_cancellation(booking_user, first_slot_start, *, now=None) -> bool:
    """External users cancel a week's slots without any penalty (full refund to the wallet) until the same Wednesday
    20:30 cut-off, so the released slots are open when internal booking for that week opens at 21:00."""
    if first_slot_start is None or not is_external_booking_user(booking_user):
        return False
    return (now or timezone.now()) <= external_reschedule_cutoff(first_slot_start)


def external_hold_refusal(booking_user, *, create_as_hold: bool, exempt: bool):
    """A ``create_as_hold`` booking skips the weekly and external quotas and is only released through an internal
    urgent request, which external users cannot raise. Refuse it for external users (unless staff act for them).
    Returns an error dict, or None when allowed."""
    if not create_as_hold or exempt or not is_external_booking_user(booking_user):
        return None
    return {
        "error": "Slots cannot be held for external users. Book an open slot or submit a special booking request.",
        "code": "EXTERNAL_HOLD_NOT_ALLOWED",
    }
