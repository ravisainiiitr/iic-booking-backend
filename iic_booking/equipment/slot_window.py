"""The slot window users see and book (``Equipment.weekly_view_time_from`` / ``weekly_view_time_to``).

One rule for the booking page, booking, alternative-slot searches and automatic placement after maintenance: a slot
counts only when it lies wholly inside the window on its own day. A slot that runs past midnight (e.g. 23:00-01:00)
ends after any "time to", even though its end time reads 01:00. No window set: every slot counts.

A time field cannot hold 24:00, so 23:59 stands for "until midnight":
- a window of 00:00-23:59 covers the whole day and is no limit at all (24-hour and overnight slots included);
- a window ending at 23:59 also takes a slot that ends exactly at midnight (e.g. 23:00-00:00).
"""

from __future__ import annotations

from datetime import time, timedelta

from django.utils import timezone

START_OF_DAY = time(0, 0)
END_OF_DAY = time(23, 59)


def _local(dt):
    return timezone.localtime(dt) if timezone.is_aware(dt) else dt


def window(equipment) -> tuple[time | None, time | None]:
    """The equipment's (from, to), or (None, None) when there is no limit (unset, or the whole day)."""
    time_from = getattr(equipment, "weekly_view_time_from", None)
    time_to = getattr(equipment, "weekly_view_time_to", None)
    if (time_from is None or time_from <= START_OF_DAY) and (time_to is None or time_to >= END_OF_DAY):
        return None, None
    return time_from, time_to


def has_window(equipment) -> bool:
    return window(equipment) != (None, None)


def slot_in_window(equipment, slot) -> bool:
    time_from, time_to = window(equipment)
    if time_from is None and time_to is None:
        return True
    if not slot.start_datetime or not slot.end_datetime:
        return False
    start, end = _local(slot.start_datetime), _local(slot.end_datetime)
    if time_from is not None and start.time() < time_from:
        return False
    if time_to is not None:
        ends_at_midnight = end.date() == start.date() + timedelta(days=1) and end.time() == START_OF_DAY
        if time_to >= END_OF_DAY and ends_at_midnight:
            return True
        if end.date() > start.date() or end.time() > time_to:
            return False
    return True
