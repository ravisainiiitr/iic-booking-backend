"""
Equipment utilization factor: booked slot hours ÷ available slot hours.

Both hours count only slot time that falls inside the equipment's weekly view window ("Weekly view from / to
(24h)", local IST time) on working days. Weekends (Saturday and Sunday, ``UTILIZATION_WEEKEND_DAYS``) and active
institute holidays are not working days. A slot that crosses the window edge or midnight is split per local day
and clipped, so a 24-hour slot with a 09:00-17:30 window counts 8.5 hours per working day.

Available slots are AVAILABLE, BOOKED, BOOKING_NOT_UTILIZED, UNDER_MAINTENANCE, SCHEDULED_MAINT and
OPERATOR_ABSENT; BLOCKED, NOT_AVAILABLE and RESERVED_EXTERNAL slots and test-account bookings are left out of
both hours. Booked slots are BOOKED slots. The factor is ``None`` when there are no available hours.

Window fallback: the slot's own equipment, then its multi-mode parent, then ``UTILIZATION_DEFAULT_VIEW_WINDOW``
(``("HH:MM", "HH:MM")``), else the whole day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from django.conf import settings
from django.utils import timezone

from .models import DailySlot, Equipment, Holiday, SlotStatus

UTILIZATION_FORMULA = "Booked hours ÷ available hours within weekly view window, excluding weekends and holidays"

AVAILABLE_SLOT_STATUSES = frozenset(
    {
        SlotStatus.AVAILABLE,
        SlotStatus.BOOKED,
        SlotStatus.BOOKING_NOT_UTILIZED,
        SlotStatus.UNDER_MAINTENANCE,
        SlotStatus.SCHEDULED_MAINTENANCE,
        SlotStatus.OPERATOR_ABSENT,
    }
)
_BOOKING_STATUSES = frozenset({SlotStatus.BOOKED, SlotStatus.BOOKING_NOT_UTILIZED})


def _parse_time(value) -> Optional[time]:
    if value is None or isinstance(value, time):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text in ("24:00", "24:00:00"):
        return time(0, 0)
    return time.fromisoformat(text)


@dataclass(frozen=True)
class ViewWindow:
    """Daily [start, end) window in local time; ``None`` start is 00:00, ``None`` or 00:00 end is 24:00."""

    start: Optional[time] = None
    end: Optional[time] = None

    @property
    def is_full_day(self) -> bool:
        return self.start in (None, time(0, 0)) and self.end in (None, time(0, 0))

    def bounds_on(self, day: date) -> tuple[datetime, datetime]:
        tz = timezone.get_default_timezone()
        lo = timezone.make_aware(datetime.combine(day, self.start or time(0, 0)), tz)
        if self.end is None or self.end == time(0, 0):
            hi = timezone.make_aware(datetime.combine(day + timedelta(days=1), time(0, 0)), tz)
        else:
            hi = timezone.make_aware(datetime.combine(day, self.end), tz)
        return lo, hi

    def label(self) -> str:
        if self.is_full_day:
            return "00:00–24:00"
        end = "24:00" if self.end in (None, time(0, 0)) else self.end.strftime("%H:%M")
        return f"{(self.start or time(0, 0)).strftime('%H:%M')}–{end}"


def _own_window(equipment) -> Optional[ViewWindow]:
    if equipment is None:
        return None
    w_from = getattr(equipment, "weekly_view_time_from", None)
    w_to = getattr(equipment, "weekly_view_time_to", None)
    if w_from is None and w_to is None:
        return None
    return ViewWindow(w_from, w_to)


def default_view_window() -> ViewWindow:
    raw = getattr(settings, "UTILIZATION_DEFAULT_VIEW_WINDOW", None)
    if not raw:
        return ViewWindow()
    w_from, w_to = raw
    return ViewWindow(_parse_time(w_from), _parse_time(w_to))


def view_window_for(equipment, parent=None) -> ViewWindow:
    return _own_window(equipment) or _own_window(parent) or default_view_window()


@dataclass(frozen=True)
class WorkingCalendar:
    holidays: frozenset[date] = frozenset()
    weekend_days: frozenset[int] = frozenset({5, 6})

    @classmethod
    def for_range(cls, start: date, end: date) -> "WorkingCalendar":
        holidays = Holiday.objects.filter(date__gte=start, date__lte=end, is_active=True).values_list("date", flat=True)
        weekend = getattr(settings, "UTILIZATION_WEEKEND_DAYS", (5, 6))
        return cls(frozenset(holidays), frozenset(int(d) for d in weekend))

    def is_working_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend_days and day not in self.holidays


def _local(dt: datetime) -> datetime:
    tz = timezone.get_default_timezone()
    return timezone.localtime(dt, tz) if timezone.is_aware(dt) else timezone.make_aware(dt, tz)


def slot_hours(start_dt, end_dt) -> float:
    if not start_dt or not end_dt:
        return 0.0
    return max(0.0, (end_dt - start_dt).total_seconds() / 3600.0)


def window_hours(start_dt, end_dt, window: ViewWindow, calendar: WorkingCalendar) -> tuple[float, float]:
    """(working-day hours, weekend/holiday hours) of [start_dt, end_dt) inside the window, split per local day."""
    if not start_dt or not end_dt or end_dt <= start_dt:
        return 0.0, 0.0
    start_local, end_local = _local(start_dt), _local(end_dt)
    working = off = 0.0
    day = start_local.date()
    while day <= end_local.date():
        lo, hi = window.bounds_on(day)
        overlap = (min(end_local, hi) - max(start_local, lo)).total_seconds() / 3600.0
        if overlap > 0:
            if calendar.is_working_day(day):
                working += overlap
            else:
                off += overlap
        day += timedelta(days=1)
    return working, off


def utilization_ratio(booked_hours: float, available_hours: float) -> Optional[float]:
    if available_hours <= 0:
        return None
    return round(min(1.0, max(0.0, booked_hours / available_hours)), 4)


@dataclass
class UtilizationTally:
    """Per-equipment running totals; ``all_slot_*`` keep the previous all-slot formula for comparison."""

    booked_hours: float = 0.0
    available_hours: float = 0.0
    booked_hours_outside_window: float = 0.0
    all_slot_booked_hours: float = 0.0
    all_slot_hours: float = 0.0
    slots: int = 0

    def add(self, status, start_dt, end_dt, window: ViewWindow, calendar: WorkingCalendar, *, test_booking=False):
        if status not in AVAILABLE_SLOT_STATUSES or (test_booking and status in _BOOKING_STATUSES):
            return
        hours = slot_hours(start_dt, end_dt)
        working, _ = window_hours(start_dt, end_dt, window, calendar)
        self.slots += 1
        self.all_slot_hours += hours
        self.available_hours += working
        if status == SlotStatus.BOOKED:
            self.all_slot_booked_hours += hours
            self.booked_hours += working
            self.booked_hours_outside_window += max(0.0, hours - working)

    def merge(self, other: "UtilizationTally") -> "UtilizationTally":
        for name in ("booked_hours", "available_hours", "booked_hours_outside_window", "all_slot_booked_hours",
                     "all_slot_hours", "slots"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        return self

    @property
    def factor(self) -> Optional[float]:
        return utilization_ratio(self.booked_hours, self.available_hours)

    @property
    def all_slot_factor(self) -> Optional[float]:
        return utilization_ratio(self.all_slot_booked_hours, self.all_slot_hours)

    def as_dict(self) -> dict:
        return {
            "utilization_factor": self.factor,
            "booked_hours": round(self.booked_hours, 2),
            "available_hours": round(self.available_hours, 2),
            "booked_hours_outside_window": round(self.booked_hours_outside_window, 2),
        }


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def compute_utilization_by_equipment(equipment_ids: Iterable[int], start, end) -> dict[int, UtilizationTally]:
    """Tallies keyed by equipment id for ``start``..``end`` (dates, inclusive); multi-mode children fold into parents."""
    from .mode_utils import expand_equipment_ids_for_mode_rollup

    start_d, end_d = _as_date(start), _as_date(end)
    ids = [int(i) for i in equipment_ids]
    expanded, rollup = expand_equipment_ids_for_mode_rollup(ids)
    tallies = {rollup.get(i, i): UtilizationTally() for i in ids}
    if not expanded:
        return tallies
    calendar = WorkingCalendar.for_range(start_d, end_d + timedelta(days=7))
    equipment = {
        e.equipment_id: e
        for e in Equipment.objects.filter(equipment_id__in=set(expanded) | set(rollup.values())).only(
            "equipment_id", "weekly_view_time_from", "weekly_view_time_to"
        )
    }
    windows = {
        eid: view_window_for(equipment.get(eid), equipment.get(rollup.get(eid, eid)))
        for eid in expanded
    }
    rows = (
        DailySlot.objects.filter(date__gte=start_d, date__lte=end_d, slot_master__equipment_id__in=expanded)
        .values_list("slot_master__equipment_id", "status", "start_datetime", "end_datetime",
                     "booking__user__is_test_account")
        .iterator(chunk_size=2000)
    )
    for eid, status, start_dt, end_dt, is_test in rows:
        target = rollup.get(eid, eid)
        tallies.setdefault(target, UtilizationTally()).add(
            status, start_dt, end_dt, windows[eid], calendar, test_booking=bool(is_test)
        )
    return tallies


def compute_utilization(equipment, start, end) -> dict:
    """Utilization of one equipment (instance or id, with its multi-mode children) for ``start``..``end``."""
    eid = int(getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None) or equipment)
    tallies = compute_utilization_by_equipment([eid], start, end)
    tally = next(iter(tallies.values()), UtilizationTally())
    return {"equipment_id": eid, **tally.as_dict()}
