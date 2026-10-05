"""Report working-window hours compare the slot's local (IST) time with the equipment's weekly window."""

from datetime import datetime, time
from datetime import timezone as dt_timezone
from types import SimpleNamespace

from django.utils import timezone

from iic_booking.equipment.reports import _slot_in_weekly_time_window

WINDOW = SimpleNamespace(weekly_view_time_from=time(9, 0), weekly_view_time_to=time(18, 0))


def _utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


def test_utc_slot_inside_the_local_window():
    # 09:30-10:30 IST is 04:00-05:00 UTC, before 09:00 if compared in UTC.
    assert _slot_in_weekly_time_window(WINDOW, _utc(2026, 10, 5, 4, 0), _utc(2026, 10, 5, 5, 0)) is True


def test_utc_slot_outside_the_local_window():
    # 19:00-20:00 IST is 13:30-14:30 UTC, inside 09:00-18:00 if compared in UTC.
    assert _slot_in_weekly_time_window(WINDOW, _utc(2026, 10, 5, 13, 30), _utc(2026, 10, 5, 14, 30)) is False


def test_local_aware_and_naive_datetimes_and_no_window():
    start = timezone.make_aware(datetime(2026, 10, 5, 9, 0))
    end = timezone.make_aware(datetime(2026, 10, 5, 18, 0))
    assert _slot_in_weekly_time_window(WINDOW, start, end) is True
    assert _slot_in_weekly_time_window(WINDOW, datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 9, 0)) is False
    no_window = SimpleNamespace(weekly_view_time_from=None, weekly_view_time_to=None)
    assert _slot_in_weekly_time_window(no_window, _utc(2026, 10, 5, 20, 0), _utc(2026, 10, 5, 21, 0)) is True
