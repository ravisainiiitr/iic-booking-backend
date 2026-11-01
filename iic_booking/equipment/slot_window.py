"""The slot window users see and book (``Equipment.weekly_view_time_from`` / ``weekly_view_time_to``).

One rule for the booking page, booking, alternative-slot searches and automatic placement after maintenance: a slot
counts only when it lies wholly inside the window on its own day. A slot that runs past midnight (e.g. 23:00-00:00)
ends after any "time to", even though its end time reads 00:00. No window set: every slot counts.
"""

from __future__ import annotations

from django.utils import timezone


def _local(dt):
    return timezone.localtime(dt) if timezone.is_aware(dt) else dt


def slot_in_window(equipment, slot) -> bool:
    time_from = getattr(equipment, "weekly_view_time_from", None)
    time_to = getattr(equipment, "weekly_view_time_to", None)
    if time_from is None and time_to is None:
        return True
    if not slot.start_datetime or not slot.end_datetime:
        return False
    start, end = _local(slot.start_datetime), _local(slot.end_datetime)
    if time_from is not None and start.time() < time_from:
        return False
    if time_to is not None and (end.date() > start.date() or end.time() > time_to):
        return False
    return True
