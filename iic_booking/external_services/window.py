"""The central external booking window: which dates external users may book ordinarily, computed from the
configuration and a clock (never from whether a scheduled task has run).

Definitions (all in the portal time zone, Asia/Kolkata):

* The policy is in force when ``config.enabled``; otherwise ``external_window`` returns ``enabled=False`` and
  callers keep the previous per-equipment rule.
* Before ``initial_open_at`` nothing is open (``is_open=False``).
* Weekly expansions are the instants with the configured weekday and time. Those in ``(counting_from, at]`` count
  while ``auto_expand`` is on; ``materialized_expansions`` holds the ones counted before ``counting_from``.
  An expansion takes effect exactly at its instant (``>=``), so at 20:59:59 the previous range still applies.
* ``last_date = first_week_start + 7 * (initial_weeks + expansions + manual_weeks) - 1`` (inclusive).
* ``first_date = max(first_week_start, today + min_lead_days)`` (inclusive). Past days are never bookable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from django.utils import timezone

WEEK = timedelta(days=7)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExternalWindow:
    enabled: bool
    is_open: bool
    first_date: Optional[date]
    last_date: Optional[date]
    opens_at: Optional[datetime]  # initial opening instant
    next_expansion_at: Optional[datetime]
    next_week_start: Optional[date]  # the week that opens at next_expansion_at
    expansions: int = 0

    def contains(self, d: date) -> bool:
        return bool(self.is_open and self.first_date and self.last_date and self.first_date <= d <= self.last_date)

    def as_dict(self) -> dict:
        def iso(v):
            return v.isoformat() if v else None

        return {
            "enabled": self.enabled,
            "is_open": self.is_open,
            "first_date": iso(self.first_date),
            "last_date": iso(self.last_date),
            "opens_at": iso(timezone.localtime(self.opens_at)) if self.opens_at else None,
            "next_expansion_at": iso(timezone.localtime(self.next_expansion_at)) if self.next_expansion_at else None,
            "next_week_start": iso(self.next_week_start),
            "next_week_end": iso(self.next_week_start + timedelta(days=6)) if self.next_week_start else None,
            "timezone": timezone.get_current_timezone_name(),
        }


DISABLED = ExternalWindow(False, False, None, None, None, None, None)


def _local(dt: datetime) -> datetime:
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return timezone.localtime(dt)


def first_instant_after(after: datetime, weekday: int, at_time: time) -> datetime:
    """The first instant strictly after ``after`` that falls on ``weekday`` at local ``at_time``."""
    local = _local(after)
    days = (int(weekday) - local.weekday()) % 7
    candidate = timezone.make_aware(
        datetime.combine(local.date() + timedelta(days=days), at_time), timezone.get_current_timezone()
    )
    if candidate <= local:
        candidate = timezone.make_aware(
            datetime.combine(candidate.date() + WEEK, at_time), timezone.get_current_timezone()
        )
    return candidate


def count_instants(start_exclusive: datetime, end_inclusive: datetime, weekday: int, at_time: time) -> int:
    """How many weekly instants fall in ``(start_exclusive, end_inclusive]``. Weeks are calendar weeks in the
    local zone, so the count is exact across month and year ends (IST has no daylight saving)."""
    if end_inclusive <= start_exclusive:
        return 0
    first = first_instant_after(start_exclusive, weekday, at_time)
    if first > _local(end_inclusive):
        return 0
    # Count by local calendar dates rather than elapsed seconds.
    days = (_local(end_inclusive).date() - first.date()).days
    count = days // 7 + 1
    last = timezone.make_aware(
        datetime.combine(first.date() + timedelta(days=7 * (count - 1)), at_time), timezone.get_current_timezone()
    )
    if last > _local(end_inclusive):
        count -= 1
    return count


def counted_expansions(config, at: datetime) -> int:
    counted = int(config.materialized_expansions or 0)
    if config.auto_expand:
        start = config.counting_from or config.initial_open_at
        if start is not None:
            counted += count_instants(start, at, config.expansion_weekday, config.expansion_time)
    return counted


def is_configured(config) -> bool:
    return bool(config.initial_open_at and config.first_week_start and config.initial_weeks)


def compute(config, at: Optional[datetime] = None, *, force_enabled: bool = False) -> ExternalWindow:
    """The window for ``config`` at ``at``. ``force_enabled`` computes it as if enabled (previews)."""
    at = _local(at or timezone.now())
    if not (config.enabled or force_enabled):
        return DISABLED
    if not is_configured(config):  # enabled without dates: fail closed
        return ExternalWindow(True, False, None, None, config.initial_open_at, None, None)
    opens_at = config.initial_open_at
    if at < _local(opens_at):
        return ExternalWindow(True, False, None, None, opens_at, None, config.first_week_start, 0)
    expansions = counted_expansions(config, at)
    weeks = int(config.initial_weeks) + expansions + int(config.manual_weeks or 0)
    last = config.first_week_start + timedelta(days=7 * weeks - 1)
    first = max(config.first_week_start, at.date() + timedelta(days=int(config.min_lead_days or 0)))
    next_at = None
    next_week = None
    if config.auto_expand:
        # The next instant that will be counted: strictly after both now and ``counting_from``.
        anchor = max(at, _local(config.counting_from or opens_at))
        next_at = first_instant_after(anchor, config.expansion_weekday, config.expansion_time)
        next_week = last + timedelta(days=1)
    return ExternalWindow(True, True, first, last, opens_at, next_at, next_week, expansions)


def external_window(at: Optional[datetime] = None) -> ExternalWindow:
    """The window now. Reads the configuration without creating it, inside a savepoint: if the table is not there
    yet (code deployed before its migration runs) the policy counts as disabled, so booking keeps the previous rule
    instead of failing."""
    from django.db import DatabaseError, transaction

    from .models import ExternalBookingWindowConfig

    try:
        with transaction.atomic():
            config = ExternalBookingWindowConfig.objects.filter(pk=1).first()
    except DatabaseError:
        logger.warning("external booking window configuration unavailable; treating the window as disabled",
                       exc_info=True)
        return DISABLED
    return DISABLED if config is None else compute(config, at)


def date_opens_at(window: ExternalWindow, d: date) -> Optional[datetime]:
    """When ``d`` becomes bookable for external users under ``window`` (None when already open, in the past or not
    reachable by the weekly expansion)."""
    if not window.enabled or window.contains(d):
        return None
    if not window.is_open:
        # Before the initial opening every date from the first week on opens then or with a later expansion;
        # the precise instant needs the configuration, so report the opening itself.
        return window.opens_at if window.next_week_start and d >= window.next_week_start else None
    if window.last_date and d > window.last_date and window.next_expansion_at:
        weeks_needed = -(-(d - window.last_date).days // 7)  # ceiling division
        return window.next_expansion_at + WEEK * (weeks_needed - 1)
    return None


def is_external_booking_user(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return bool(user is not None and UserType.is_external_user(getattr(user, "user_type", None)))


class ExternalWindowClosed(Exception):
    def __init__(self, message: str, *, dates: list[date], window: ExternalWindow):
        super().__init__(message)
        self.message = message
        self.dates = dates
        self.window = window

    def payload(self) -> dict:
        return {
            "error": self.message,
            "code": "EXTERNAL_WINDOW_CLOSED",
            "external_window": self.window.as_dict(),
            "dates_outside_window": [d.isoformat() for d in self.dates],
            "special_request_available": True,
        }


def closed_message(window: ExternalWindow, outside: list[date]) -> str:
    def fmt(d: date) -> str:
        return d.strftime("%a %d %b %Y")

    if not window.is_open:
        when = timezone.localtime(window.opens_at).strftime("%d %b %Y %I:%M %p IST") if window.opens_at else "later"
        return (
            f"External bookings are not open yet. Booking opens on {when}. "
            "You can submit a special booking request instead."
        )
    return (
        f"{fmt(min(outside))} is outside the dates open to external users "
        f"({fmt(window.first_date)} to {fmt(window.last_date)}). "
        "You can submit a special booking request for dates outside this range."
    )


def check_dates(dates: Iterable[date], at: Optional[datetime] = None, *, window: Optional[ExternalWindow] = None):
    """Raise ``ExternalWindowClosed`` when the policy is on and any date is outside the open range."""
    window = window or external_window(at)
    if not window.enabled:
        return window
    dates = sorted({d for d in dates if d is not None})
    if not dates:  # nothing to check against: refuse rather than let an undated booking through
        raise ExternalWindowClosed(closed_message(window, [timezone.localdate()]), dates=[], window=window)
    outside = [d for d in dates if not window.contains(d)]
    if outside:
        raise ExternalWindowClosed(closed_message(window, outside), dates=outside, window=window)
    return window


def check_booking_dates(booking_user, dates: Iterable[date], *, exempt: bool, at: Optional[datetime] = None):
    """The server-side guard for an ordinary booking by ``booking_user``. Internal users and exempt staff
    (Main Administrator / OIC acting for a user) are never affected."""
    if exempt or not is_external_booking_user(booking_user):
        return None
    return check_dates(dates, at)


def legacy_bounds_or_policy(equipment, at=None):
    """``(min_date, max_date, before_reference)`` for external users: the central window when it is enabled
    (``None`` otherwise, so the caller keeps the previous rule). A closed window gives an empty range."""
    window = external_window(at)
    if not window.enabled:
        return None
    if not window.is_open or not window.first_date or not window.last_date:
        anchor = timezone.localdate()
        return anchor, anchor - timedelta(days=1), True
    return window.first_date, window.last_date, False
