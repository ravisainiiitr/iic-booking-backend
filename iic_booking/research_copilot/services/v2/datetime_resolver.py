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


MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
            ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
            ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b\.?(?:,?\s*(\d{{4}}))?")
_MONTH_DAY_RE = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?")
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b")


def _local_today() -> date:
    return timezone.localdate()


def _safe_date(year: int | None, month: int, day: int, today: date) -> date | None:
    """A date without a year that has already passed means the next occurrence."""
    try:
        d = date(year or today.year, month, day)
    except ValueError:
        return None
    if year is None and d < today:
        try:
            d = date(today.year + 1, month, day)
        except ValueError:
            return None
    return d


def _explicit_date(lower: str, today: date) -> date | None:
    m = _ISO_DATE_RE.search(lower)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
    m = _DAY_MONTH_RE.search(lower)
    if m:
        return _safe_date(int(m.group(3)) if m.group(3) else None, MONTHS[m.group(2)], int(m.group(1)), today)
    m = _MONTH_DAY_RE.search(lower)
    if m:
        return _safe_date(int(m.group(3)) if m.group(3) else None, MONTHS[m.group(1)], int(m.group(2)), today)
    m = _NUMERIC_DATE_RE.search(lower)
    if m:
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
        return _safe_date(year, int(m.group(2)), int(m.group(1)), today)
    return None


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

    explicit = _explicit_date(lower, today)
    if explicit is not None:
        return DateWindow(explicit, explicit, after_time, explicit.strftime("%a %d %b %Y"))
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
