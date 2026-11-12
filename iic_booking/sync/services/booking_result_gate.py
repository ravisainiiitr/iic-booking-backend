"""
Whether a booking may receive result files from a Department Sync Agent.

Terminal bookings (refunded, cancelled, not utilised) never take results. Suspended bookings
(disruption, maintenance, hold, operator unavailable, awaiting the user's disruption choice) do
not take them *yet*: the DSA holds those files and sends them once the booking resumes. COMPLETED
and PROCESSING keep accepting late files.

This backs up the DSA's own rule (DSA 1.0.7 BookingUploadGate) on the server.
"""

from __future__ import annotations

from iic_booking.equipment.models import BookingStatus
from iic_booking.sync.exceptions import SyncControlPlaneError

TERMINAL_STATUSES = frozenset(
    {
        BookingStatus.CANCELLED,
        BookingStatus.REFUNDED,
        BookingStatus.BOOKING_NOT_UTILIZED,
    }
)

SUSPENDED_STATUSES = frozenset(
    {
        BookingStatus.DISRUPTION_PENDING,
        BookingStatus.UNDER_MAINTENANCE,
        BookingStatus.OTHER_DISRUPTION,
        BookingStatus.HOLD,
        BookingStatus.ABSENT,
    }
)


class BookingNotAcceptingResultsError(SyncControlPlaneError):
    code = "BOOKING_NOT_ACCEPTING_RESULTS"
    status_code = 409
    default_message = "This booking is not accepting result files."


def blocked_reason(status: str | None) -> str | None:
    """'terminal', 'suspended' or None (accepting)."""
    if status in TERMINAL_STATUSES:
        return "terminal"
    if status in SUSPENDED_STATUSES:
        return "suspended"
    return None


def ensure_booking_accepts_results(booking) -> None:
    """Raise BookingNotAcceptingResultsError when the booking cannot take results now."""
    if booking is None:
        return
    status = getattr(booking, "status", None)
    reason = blocked_reason(status)
    if reason == "terminal":
        raise BookingNotAcceptingResultsError(
            f"Booking is {status}; result files are not accepted.",
        )
    if reason == "suspended":
        raise BookingNotAcceptingResultsError(
            f"Booking is {status} (suspended); the DSA should hold result files until it resumes.",
        )
