"""Deterministic date/time window resolution (Asia/Kolkata)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone


WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class DateWindow:
    start_date: date
    end_date: date  # inclusive
    after_time: time | None = None
    label: str = ""
    ambiguous: bool = False


def _local_today() -> date:
    return timezone.localdate()


def resolve_date_window(text: str) -> DateWindow:
    lower = (text or "").lower()
    today = _local_today()
    after_time = None
    m = re.search(r"after\s+(\d{1,2})\s*(am|pm)?", lower)
    if m:
        hour = int(m.group(1))
        ampm = (m.group(2) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if not ampm and hour <= 7:
            # bare "after 2" in lab context → afternoon
            hour = hour + 12 if hour < 12 else hour
        after_time = time(hour=min(hour, 23), minute=0)

    if "tomorrow" in lower:
        d = today + timedelta(days=1)
        return DateWindow(d, d, after_time, "tomorrow")
    if "today" in lower:
        return DateWindow(today, today, after_time, "today")
    if "next week" in lower:
        # Monday of next week → Sunday
        days_ahead = (7 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        start = today + timedelta(days=days_ahead)
        return DateWindow(start, start + timedelta(days=6), after_time, "next week")
    if "this week" in lower or "week" in lower:
        # Remaining days of this calendar week (Mon–Sun), including today
        start = today
        end = today + timedelta(days=(6 - today.weekday()))
        if end < start:
            end = start
        return DateWindow(start, end, after_time, "this week")

    for name, wd in WEEKDAYS.items():
        if name in lower:
            days = (wd - today.weekday()) % 7
            if days == 0 and "next" in lower:
                days = 7
            if days == 0:
                days = 7  # "Friday" when today is Friday → next Friday for booking intent
            d = today + timedelta(days=days)
            return DateWindow(d, d, after_time, name)

    # Default for slot searches without date: this week remaining
    end = today + timedelta(days=6)
    return DateWindow(today, end, after_time, "next 7 days")
