"""Server-side guards called from the existing booking entry points (kept small so those views change little)."""

from __future__ import annotations

from datetime import datetime, timedelta

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


# External users reschedule or cancel without penalty up to 48 hours before the first slot (2 Nov 2026; the hours follow
# ``ExternalServicesSettings.cancellation_free_hours``). Later, only the Officer in Charge or the Main Administrator
# reschedules (the user can message the lab from the booking details page).
EXTERNAL_CUTOFF_HOURS = 48


def _cutoff_hours() -> int:
    try:
        from .models import ExternalServicesSettings

        return int(ExternalServicesSettings.load().cancellation_free_hours or EXTERNAL_CUTOFF_HOURS)
    except Exception:  # noqa: BLE001 - settings unavailable: the policy default
        return EXTERNAL_CUTOFF_HOURS


def external_reschedule_cutoff(first_slot_start) -> datetime:
    """48 hours (the free-cancellation hours) before ``first_slot_start``."""
    return first_slot_start - timedelta(hours=_cutoff_hours())


def external_reschedule_refusal(booking_user, first_slot_start, *, now=None):
    """Error payload when an external user reschedules their own booking after the weekly cut-off, else None."""
    if first_slot_start is None or not is_external_booking_user(booking_user):
        return None
    cutoff = external_reschedule_cutoff(first_slot_start)
    if (now or timezone.now()) <= cutoff:
        return None
    return {
        "error": (
            f"This booking could be rescheduled by you until {timezone.localtime(cutoff):%A %d %b %Y, %H:%M} "
            f"({_cutoff_hours()} hours before the slot). Please send a message to the lab from the booking details "
            "page; the Officer in Charge or the Main Administrator can reschedule it."
        ),
        "code": "EXTERNAL_RESCHEDULE_CUTOFF",
        "cutoff": timezone.localtime(cutoff).isoformat(),
    }


def external_free_cancellation(booking_user, first_slot_start, *, now=None) -> bool:
    """External users cancel without any penalty (full refund to the wallet) up to 48 hours before the first slot."""
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
