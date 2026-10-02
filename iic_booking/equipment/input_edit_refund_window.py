"""Whether a lower charge from the booking user's own input edit is refunded straight away.

The window is the same one that lets the user cancel or reschedule the booking themselves:
until ``equipment.reschedule_hours_threshold`` hours (48 when not set) before the first booked slot.
Bookings flagged for maintenance disruption may be cancelled at any time, so the window stays open
for them. After the window, the refund waits for the Officer In Charge to confirm it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

from django.utils import timezone

DEFAULT_RESCHEDULE_HOURS_THRESHOLD = 48


def self_service_cutoff(booking) -> Optional[datetime]:
    """Last moment the booking user may cancel or reschedule; None when no slot start is known."""
    starts = [s.start_datetime for s in booking.daily_slots.all() if s.start_datetime is not None]
    if not starts:
        return None
    equipment = getattr(booking, "equipment", None)
    hours = int(getattr(equipment, "reschedule_hours_threshold", None) or DEFAULT_RESCHEDULE_HOURS_THRESHOLD)
    return min(starts) - timedelta(hours=hours)


def instant_refund_window(booking, now=None) -> Tuple[bool, Optional[datetime]]:
    """``(open, cutoff)``: ``open`` is True while a lower charge is refunded without OIC confirmation."""
    if getattr(booking, "maintenance_disruption_flag", False):
        return True, None
    cutoff = self_service_cutoff(booking)
    if cutoff is None:
        return False, None
    return (now or timezone.now()) <= cutoff, cutoff
