"""
Date and time expressions for the Booking Assistant, resolved in portal time (IST).

Covers "today", "tomorrow", "day after tomorrow", "in 3 days", weekday names ("friday",
"next monday" = Monday of next calendar week), "this/next week", "weekend", explicit dates
(2026-10-05, 5/10, 5 Oct, Oct 5th) and times of day ("morning", "afternoon", "evening",
"after 2pm", "before 11", "at 10:30", "between 10 and 1"). A bare hour 1-7 without am/pm means pm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any

from django.utils import timezone

MAX_RANGE_DAYS = 14
DEFAULT_RANGE_DAYS = 7

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_MO = "|".join(sorted(_MONTHS, key=len, reverse=True))
_TIME = (
    r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\w/])"
    r"(?!\s*(?:samples?|specimens?|hours?|hrs?|mins?|minutes?|days?|weeks?|slots?|nos?\b|x\b|%))"
)

_RE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_RE_DMY_DASH = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")
_RE_DMY = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_RE_D_MON = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MO})\b\.?(?:,?\s*(\d{{4}}))?")
_RE_MON_D = re.compile(rf"\b({_MO})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?")
_RE_DAY_AFTER = re.compile(r"\b(day after tomorrow|day after tmrw|overmorrow)\b")
_RE_TOMORROW = re.compile(r"\b(tomorrow|tmrw|tmr|tommorow|tomorow|tommorrow|tomorrw)\b")
_RE_TODAY = re.compile(r"\b(today|tonight|right now|asap|now)\b")
_RE_IN_DAYS = re.compile(r"\bin\s+(\d{1,2})\s+days?\b")
_RE_NEXT_N_DAYS = re.compile(r"\b(?:next|coming)\s+(\d{1,2})\s+days\b")
_RE_FEW_DAYS = re.compile(r"\b(?:next|coming)\s+few\s+days\b")
_RE_NEXT_WEEK = re.compile(r"\b(next week|coming week)\b")
_RE_THIS_WEEK = re.compile(r"\b(this week|later this week|rest of the week)\b")
_RE_WEEKEND = re.compile(r"\b(next weekend|this weekend|weekend)\b")
_RE_WEEKDAY = re.compile(rf"\b(?:(next|this|coming)\s+)?(?:on\s+)?({_WD})\b\.?")

_RE_BETWEEN = re.compile(rf"\b(?:between|from)\s+{_TIME}\s*(?:and|to|till|until|-)\s*{_TIME}")
_RE_RANGE = re.compile(rf"(?<![\d:/.-]){_TIME}\s*(?:-|to)\s*{_TIME}")
_RE_AFTER = re.compile(rf"\b(?:after|from|post|later than|not before)\s+{_TIME}")
_RE_BEFORE = re.compile(rf"\b(?:before|by|until|till|earlier than|no later than)\s+{_TIME}")
_RE_AT = re.compile(rf"(?:\b(?:at|around|about|near)\s+|@\s*){_TIME}")
_RE_BARE = re.compile(rf"(?<![\d:/.-])\b{_TIME}")
_RE_NOON = re.compile(r"\b(noon|midday)\b")
_RE_PERIOD = re.compile(r"\b(morning|forenoon|afternoon|evening|night)\b")

_PERIOD_BOUNDS = {
    "morning": (None, time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), None),
}


@dataclass
class When:
    start_date: date
    end_date: date
    after: time | None = None
    before: time | None = None
    at: time | None = None
    period: str | None = None
    explicit_date: bool = False
    explicit_time: bool = False
    label: str = ""
    spans: list[tuple[int, int]] = field(default_factory=list)
    past: bool = False

    @property
    def explicit(self) -> bool:
        return self.explicit_date or self.explicit_time

    @property
    def single_day(self) -> bool:
        return self.start_date == self.end_date

    def accepts(self, start_local_time: time) -> bool:
        """Whether a slot starting at this local time satisfies the time-of-day constraints."""
        if self.after and start_local_time < self.after:
            return False
        if self.before and start_local_time >= self.before:
            return False
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "start": self.start_date.isoformat(),
            "end": self.end_date.isoformat(),
            "after": self.after.strftime("%H:%M") if self.after else None,
            "before": self.before.strftime("%H:%M") if self.before else None,
            "at": self.at.strftime("%H:%M") if self.at else None,
            "period": self.period,
        }


def _today() -> date:
    return timezone.localdate()


def normalize(text: str) -> str:
    lower = (text or "").lower()
    lower = re.sub(r"[\u2010-\u2015\u2212]", " - ", lower)
    return re.sub(r"\s+", " ", lower).strip()


def _to_time(hour: str | None, minute: str | None, ampm: str | None) -> time | None:
    if hour is None:
        return None
    h = int(hour)
    m = int(minute) if minute else 0
    marker = (ampm or "").replace(".", "")
    if marker in {"pm", "p"} and h < 12:
        h += 12
    elif marker in {"am", "a"} and h == 12:
        h = 0
    elif not marker and 1 <= h <= 7:
        h += 12
    if h > 23 or m > 59:
        return None
    return time(h, m)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _roll_year(d: date | None, today: date, year_given: bool) -> date | None:
    if d is None or year_given or d >= today:
        return d
    return _safe_date(d.year + 1, d.month, d.day)


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for s, e in spans:
        for i in range(s, min(e, len(chars))):
            chars[i] = " "
    return "".join(chars)


def _day_label(d: date, today: date) -> str:
    if d == today:
        return f"today ({d:%a %d %b})"
    if d == today + timedelta(days=1):
        return f"tomorrow ({d:%a %d %b})"
    return f"{d:%a %d %b}"


def _range_label(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start:%d}–{end:%d %b}"
    return f"{start:%d %b}–{end:%d %b}"


def _resolve_dates(t: str, today: date) -> tuple[date | None, date | None, str, list[tuple[int, int]]]:
    """Return (start, end, kind_label, spans). kind_label names ranges like "next week"."""
    for rx in (_RE_ISO,):
        m = rx.search(t)
        if m:
            d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d:
                return d, d, "", [m.span()]
    m = _RE_DMY_DASH.search(t)
    if m:
        d = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d:
            return d, d, "", [m.span()]
    m = _RE_D_MON.search(t)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        d = _roll_year(_safe_date(year, _MONTHS[m.group(2)], int(m.group(1))), today, bool(m.group(3)))
        if d:
            return d, d, "", [m.span()]
    m = _RE_MON_D.search(t)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        d = _roll_year(_safe_date(year, _MONTHS[m.group(1)], int(m.group(2))), today, bool(m.group(3)))
        if d:
            return d, d, "", [m.span()]
    m = _RE_DMY.search(t)
    if m:
        raw_year = m.group(3)
        year = int(raw_year) if raw_year else today.year
        if raw_year and year < 100:
            year += 2000
        d = _roll_year(_safe_date(year, int(m.group(2)), int(m.group(1))), today, bool(raw_year))
        if d:
            return d, d, "", [m.span()]
    m = _RE_DAY_AFTER.search(t)
    if m:
        d = today + timedelta(days=2)
        return d, d, "", [m.span()]
    m = _RE_TOMORROW.search(t)
    if m:
        d = today + timedelta(days=1)
        return d, d, "", [m.span()]
    m = _RE_IN_DAYS.search(t)
    if m:
        d = today + timedelta(days=min(int(m.group(1)), 60))
        return d, d, "", [m.span()]
    m = _RE_NEXT_N_DAYS.search(t)
    if m:
        n = max(1, min(int(m.group(1)), MAX_RANGE_DAYS))
        return today, today + timedelta(days=n - 1), f"the next {n} days", [m.span()]
    m = _RE_FEW_DAYS.search(t)
    if m:
        return today, today + timedelta(days=3), "the next few days", [m.span()]
    m = _RE_NEXT_WEEK.search(t)
    if m:
        start = today + timedelta(days=7 - today.weekday())
        return start, start + timedelta(days=6), "next week", [m.span()]
    m = _RE_THIS_WEEK.search(t)
    if m:
        end = today + timedelta(days=6 - today.weekday())
        return today, end, "this week", [m.span()]
    m = _RE_WEEKEND.search(t)
    if m:
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:
            saturday = today - timedelta(days=1)
        if m.group(1) == "next weekend":
            saturday += timedelta(days=7)
        start = max(saturday, today)
        return start, saturday + timedelta(days=1), "the weekend", [m.span()]
    m = _RE_WEEKDAY.search(t)
    if m:
        target = _WEEKDAYS[m.group(2)]
        if m.group(1) == "next":
            start_next_week = today + timedelta(days=7 - today.weekday())
            d = start_next_week + timedelta(days=target)
        else:
            d = today + timedelta(days=(target - today.weekday()) % 7)
        return d, d, "", [m.span()]
    m = _RE_TODAY.search(t)
    if m:
        if m.group(1) in {"asap", "now", "right now"}:
            return today, today + timedelta(days=MAX_RANGE_DAYS - 1), "the earliest dates", [m.span()]
        return today, today, "", [m.span()]
    return None, None, "", []


def parse_when(text: str, *, today: date | None = None) -> When:
    today = today or _today()
    t = normalize(text)
    start, end, kind, spans = _resolve_dates(t, today)
    explicit_date = start is not None
    if start is None:
        start, end = today, today + timedelta(days=DEFAULT_RANGE_DAYS - 1)
    past = bool(end and end < today)
    if (end - start).days > MAX_RANGE_DAYS - 1:
        end = start + timedelta(days=MAX_RANGE_DAYS - 1)

    rest = _mask(t, spans)
    after = before = at = None
    period = None
    time_spans: list[tuple[int, int]] = []

    m = _RE_BETWEEN.search(rest)
    if m:
        after, before = _to_time(*m.group(1, 2, 3)), _to_time(*m.group(4, 5, 6))
        time_spans.append(m.span())
    else:
        m = _RE_RANGE.search(rest)
        if m and (m.group(3) or m.group(6) or m.group(2) or m.group(5)):
            after, before = _to_time(*m.group(1, 2, 3)), _to_time(*m.group(4, 5, 6))
            time_spans.append(m.span())
    if after and before and before <= after and before.hour < 12:
        before = time(min(before.hour + 12, 23), before.minute)
    if not time_spans:
        m = _RE_AFTER.search(rest)
        if m:
            after = _to_time(*m.group(1, 2, 3))
            time_spans.append(m.span())
        m = _RE_BEFORE.search(rest)
        if m:
            before = _to_time(*m.group(1, 2, 3))
            time_spans.append(m.span())
    if not time_spans:
        m = _RE_AT.search(rest)
        if m:
            at = _to_time(*m.group(1, 2, 3))
            time_spans.append(m.span())
        else:
            for bm in _RE_BARE.finditer(rest):
                if ":" in bm.group(0) or bm.group(3):
                    at = _to_time(*bm.group(1, 2, 3))
                    if at:
                        time_spans.append(bm.span())
                        break
    m = _RE_NOON.search(rest)
    if m and not (after or before or at):
        at = time(12, 0)
        time_spans.append(m.span())
    pm = _RE_PERIOD.search(rest)
    if pm:
        word = pm.group(1)
        period = {"forenoon": "morning", "night": "evening"}.get(word, word)
        time_spans.append(pm.span())
        lo, hi = _PERIOD_BOUNDS[period]
        if not (after or before or at):
            after, before = lo, hi
    if text and "tonight" in t and not (after or before or at):
        period, after = "evening", time(17, 0)

    when = When(
        start_date=start,
        end_date=end,
        after=after,
        before=before,
        at=at,
        period=period,
        explicit_date=explicit_date,
        explicit_time=bool(after or before or at or period),
        spans=spans + time_spans,
        past=past,
    )
    when.label = describe(when, today=today, kind=kind)
    return when


def describe(when: When, *, today: date | None = None, kind: str = "") -> str:
    today = today or _today()
    if when.single_day:
        label = _day_label(when.start_date, today)
    elif kind:
        label = f"{kind} ({_range_label(when.start_date, when.end_date)})"
    elif when.explicit_date:
        label = _range_label(when.start_date, when.end_date)
    else:
        label = "the next 7 days"
    if when.at:
        label += f", around {when.at:%H:%M}"
    elif when.period and (when.after, when.before) == _PERIOD_BOUNDS.get(when.period):
        label += f", {when.period}"
    elif when.after and when.before:
        label += f", {when.after:%H:%M}–{when.before:%H:%M}"
    elif when.after:
        label += f", after {when.after:%H:%M}"
    elif when.before:
        label += f", before {when.before:%H:%M}"
    return label


def strip_when(text: str, when: When) -> str:
    """Text with the date/time expressions removed (for equipment-name extraction)."""
    return re.sub(r"\s+", " ", _mask(normalize(text), when.spans)).strip()


def when_from_payload(data: dict[str, Any] | None, *, today: date | None = None) -> When | None:
    """Rebuild a window from a card action payload (dates are re-clamped to today..+13)."""
    if not isinstance(data, dict):
        return None
    today = today or _today()

    def _d(v):
        try:
            return date.fromisoformat(str(v))
        except (TypeError, ValueError):
            return None

    def _t(v):
        try:
            hh, mm = str(v).split(":")[:2]
            return time(int(hh), int(mm))
        except (TypeError, ValueError):
            return None

    start = _d(data.get("start"))
    if start is None:
        return None
    end = _d(data.get("end")) or start
    start = max(start, today)
    end = max(end, start)
    if (end - start).days > MAX_RANGE_DAYS - 1:
        end = start + timedelta(days=MAX_RANGE_DAYS - 1)
    period = data.get("period") if data.get("period") in _PERIOD_BOUNDS else None
    when = When(
        start_date=start,
        end_date=end,
        after=_t(data.get("after")) if data.get("after") else None,
        before=_t(data.get("before")) if data.get("before") else None,
        at=_t(data.get("at")) if data.get("at") else None,
        period=period,
        explicit_date=True,
        explicit_time=bool(data.get("after") or data.get("before") or data.get("at") or period),
    )
    when.label = describe(when, today=today)
    return when


def day_window(d: date, *, today: date | None = None) -> When:
    today = today or _today()
    when = When(start_date=d, end_date=d, explicit_date=True)
    when.label = describe(when, today=today)
    return when
