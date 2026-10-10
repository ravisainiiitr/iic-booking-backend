"""
Equipment utilization factor: booked slot hours ÷ available slot hours.

Both hours count only slot time that falls inside the equipment's weekly view window ("Weekly view from / to
(24h)", local IST time) on working days. Weekends (Saturday and Sunday, ``UTILIZATION_WEEKEND_DAYS``) and active
institute holidays are not working days. A slot that crosses the window edge or midnight is split per local day
and clipped, so a 24-hour slot with a 09:00-17:30 window counts 8.5 hours per working day.

Available slots are AVAILABLE, BOOKED, BOOKING_NOT_UTILIZED, UNDER_MAINTENANCE, SCHEDULED_MAINT and
OPERATOR_ABSENT; BLOCKED, NOT_AVAILABLE and RESERVED_EXTERNAL slots and test-account bookings are left out of
both hours, and test equipment is left out altogether. Booked slots are BOOKED slots. The factor is ``None`` when
there are no available hours.

Window fallback: the slot's own equipment, then its multi-mode parent, then ``UTILIZATION_DEFAULT_VIEW_WINDOW``
(``("HH:MM", "HH:MM")``), else the whole day.

The period never starts before ``PORTAL_GO_LIVE_DATE`` (first bookings on the new portal) and never runs past
now (a slot in progress counts up to now); a period with no days left has no slots, so the factor is ``None``.

Available hours = booked + booking not utilized + no booking + downtime (maintenance, scheduled maintenance,
operator absent), all clipped the same way: downtime lowers the factor. "Other reasons" blocks are not offered
time and are reported separately. Completed hours (completed bookings) share the same denominator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from django.conf import settings
from django.utils import timezone

from .models import BookingStatus, DailySlot, Equipment, Holiday, SlotStatus

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


def portal_go_live_date() -> Optional[date]:
    raw = getattr(settings, "PORTAL_GO_LIVE_DATE", None)
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw).strip())


def _human(day: date) -> str:
    return day.strftime("%d %b %Y")


@dataclass(frozen=True)
class UtilizationPeriod:
    """Requested ``start``..``end`` with ``start`` moved up to the portal go-live date and ``end`` capped at today."""

    start: date
    end: date
    requested_start: date
    requested_end: Optional[date] = None
    until: Optional[datetime] = None  # "now" when the period reaches today: later slot time is not counted yet

    @property
    def is_empty(self) -> bool:
        return self.start > self.end

    def clip(self, start_dt, end_dt):
        """Slot ``(start, end)`` cut at ``until``; ``None`` when the slot has not started yet."""
        if self.until is None or end_dt is None:
            return start_dt, end_dt
        if start_dt is not None and start_dt >= self.until:
            return None
        return start_dt, min(end_dt, self.until)

    @property
    def clamped(self) -> bool:
        return self.start > self.requested_start

    @property
    def capped(self) -> bool:
        return self.requested_end is not None and self.end < self.requested_end

    def display(self) -> str:
        return "" if self.is_empty else f"{_human(self.start)} – {_human(self.end)}"

    def note(self) -> str:
        """'' when slot hours cover the requested period; otherwise the period they do cover and why."""
        go_live = portal_go_live_date()
        if self.is_empty:
            if go_live and self.requested_end is not None and self.requested_end < go_live:
                return f"Effective period for utilization: none (period is before portal go-live, {_human(go_live)})"
            return "Effective period for utilization: none (period has not started yet)"
        if not (self.clamped or self.capped):
            return ""
        reasons = []
        if self.clamped and go_live:
            reasons.append(f"portal go-live {_human(go_live)}")
        if self.capped:
            reasons.append("till current date")
        return f"Effective period for utilization: {self.display()} ({'; '.join(reasons)})"

    def as_dict(self) -> dict:
        return {
            "utilization_period_from": None if self.is_empty else self.start.isoformat(),
            "utilization_period_to": None if self.is_empty else self.end.isoformat(),
            "utilization_period_clamped": self.clamped,
            "utilization_period_display": self.display(),
            "utilization_period_note": self.note(),
            "portal_go_live_date": go_live.isoformat() if (go_live := portal_go_live_date()) else None,
        }


def utilization_period_caption(summary: dict) -> str:
    """The report's effective-period note ('' when the slot hours cover the whole requested period)."""
    return summary.get("utilization_period_note") or ""


def _now() -> datetime:
    return timezone.now()


def utilization_today() -> date:
    return timezone.localdate(_now())


def utilization_period(start, end) -> UtilizationPeriod:
    """Slot-hour period: never before ``PORTAL_GO_LIVE_DATE`` and never after now."""
    requested_start, requested_end = _as_date(start), _as_date(end)
    go_live = portal_go_live_date()
    now = _now()
    today = timezone.localdate(now)
    return UtilizationPeriod(
        max(requested_start, go_live) if go_live else requested_start,
        min(requested_end, today),
        requested_start,
        requested_end,
        now if requested_end >= today else None,
    )


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
    completed_hours: float = 0.0
    off_day_hours: float = 0.0
    slots: int = 0

    def add(self, status, start_dt, end_dt, window: ViewWindow, calendar: WorkingCalendar, *, test_booking=False,
            completed=False) -> float:
        """Count one slot; returns its available hours (0 for excluded slots). ``completed``: booking completed."""
        if status not in AVAILABLE_SLOT_STATUSES or (test_booking and status in _BOOKING_STATUSES):
            return 0.0
        hours = slot_hours(start_dt, end_dt)
        working, off = window_hours(start_dt, end_dt, window, calendar)
        self.slots += 1
        self.all_slot_hours += hours
        self.available_hours += working
        self.off_day_hours += off
        if status == SlotStatus.BOOKED:
            self.all_slot_booked_hours += hours
            self.booked_hours += working
            self.booked_hours_outside_window += max(0.0, hours - working)
            if completed:
                self.completed_hours += working
        return working

    def merge(self, other: "UtilizationTally") -> "UtilizationTally":
        for name in ("booked_hours", "available_hours", "booked_hours_outside_window", "all_slot_booked_hours",
                     "all_slot_hours", "completed_hours", "off_day_hours", "slots"):
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
            "completed_hours": round(self.completed_hours, 2),
        }


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass(frozen=True)
class CountedSlot:
    """One slot of the period, already cut at "now", with the window of its own equipment."""

    slot_id: int
    equipment_id: int
    target_id: int  # multi-mode parent (or the equipment itself)
    status: str
    start: datetime
    end: datetime
    window: ViewWindow
    test_booking: bool
    booking_id: Optional[int]
    booking_status: Optional[str]

    @property
    def completed(self) -> bool:
        return not self.test_booking and self.booking_status == BookingStatus.COMPLETED


@dataclass
class UtilizationScope:
    """Equipment, period, calendar and windows that every utilization figure is computed over."""

    period: UtilizationPeriod
    calendar: WorkingCalendar
    rollup: dict[int, int]
    expanded: list[int]
    targets: list[int]
    windows: dict[int, ViewWindow]
    test_equipment: frozenset[int] = frozenset()

    def tallies(self) -> dict[int, UtilizationTally]:
        return {t: UtilizationTally() for t in self.targets}

    def add(self, tallies: dict[int, UtilizationTally], slot: CountedSlot) -> float:
        return tallies.setdefault(slot.target_id, UtilizationTally()).add(
            slot.status, slot.start, slot.end, slot.window, self.calendar,
            test_booking=slot.test_booking, completed=slot.completed,
        )

    def slots(self):
        if not self.expanded or self.period.is_empty:
            return
        rows = (
            DailySlot.objects.filter(
                date__gte=self.period.start, date__lte=self.period.end, slot_master__equipment_id__in=self.expanded
            )
            .order_by()
            .values_list("pk", "slot_master__equipment_id", "status", "start_datetime", "end_datetime",
                         "booking_id", "booking__user__is_test_account", "booking__status")
            .iterator(chunk_size=2000)
        )
        for pk, eid, status, start_dt, end_dt, booking_id, is_test, booking_status in rows:
            if (span := self.period.clip(start_dt, end_dt)) is None:
                continue
            yield CountedSlot(pk, eid, self.rollup.get(eid, eid), status, span[0], span[1], self.windows[eid],
                              bool(is_test), booking_id, booking_status)


def utilization_scope(equipment_ids: Iterable[int], start, end, *, period=None) -> UtilizationScope:
    """
    Scope for ``start``..``end`` (dates, inclusive, from go-live, till now): multi-mode children fold into parents,
    test equipment (``iic_booking.equipment.testdata``) is left out.
    """
    from .mode_utils import expand_equipment_ids_for_mode_rollup
    from .testdata import get_test_equipment_ids

    period = period or utilization_period(start, end)
    ids = [int(i) for i in equipment_ids]
    expanded, rollup = expand_equipment_ids_for_mode_rollup(ids)
    test_ids = frozenset(get_test_equipment_ids(expanded)) if expanded else frozenset()
    expanded = [eid for eid in expanded if eid not in test_ids]
    targets = list(dict.fromkeys(rollup.get(i, i) for i in ids if i not in test_ids and rollup.get(i, i) not in test_ids))
    calendar = WorkingCalendar.for_range(period.start, max(period.start, period.end) + timedelta(days=7))
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
    return UtilizationScope(period, calendar, rollup, expanded, targets, windows, test_ids)


def compute_utilization_by_equipment(equipment_ids: Iterable[int], start, end) -> dict[int, UtilizationTally]:
    """
    Tallies keyed by equipment id (multi-mode parent) for ``start``..``end``; test equipment has no tally.
    """
    scope = utilization_scope(equipment_ids, start, end)
    tallies = scope.tallies()
    for slot in scope.slots():
        scope.add(tallies, slot)
    return tallies


def compute_utilization(equipment, start, end) -> dict:
    """Utilization of one equipment (instance or id, with its multi-mode children) for ``start``..``end``."""
    eid = int(getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None) or equipment)
    tallies = compute_utilization_by_equipment([eid], start, end)
    tally = next(iter(tallies.values()), UtilizationTally())
    return {"equipment_id": eid, **tally.as_dict(), **utilization_period(start, end).as_dict()}
