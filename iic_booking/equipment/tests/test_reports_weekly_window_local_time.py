"""Report working-window hours compare the slot's local (IST) time with the equipment's weekly window."""

from datetime import datetime, time
from datetime import timezone as dt_timezone

from django.utils import timezone

from iic_booking.equipment.utilization import ViewWindow, WorkingCalendar, window_hours

WINDOW = ViewWindow(time(9, 0), time(18, 0))
WEEKDAYS = WorkingCalendar()


def _utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


def test_utc_slot_inside_the_local_window():
    # 09:30-10:30 IST is 04:00-05:00 UTC, before 09:00 if compared in UTC.
    assert window_hours(_utc(2026, 10, 5, 4, 0), _utc(2026, 10, 5, 5, 0), WINDOW, WEEKDAYS) == (1.0, 0.0)


def test_utc_slot_outside_the_local_window():
    # 19:00-20:00 IST is 13:30-14:30 UTC, inside 09:00-18:00 if compared in UTC.
    assert window_hours(_utc(2026, 10, 5, 13, 30), _utc(2026, 10, 5, 14, 30), WINDOW, WEEKDAYS) == (0.0, 0.0)


def test_local_aware_and_naive_datetimes_and_no_window():
    start = timezone.make_aware(datetime(2026, 10, 5, 9, 0))
    end = timezone.make_aware(datetime(2026, 10, 5, 18, 0))
    assert window_hours(start, end, WINDOW, WEEKDAYS) == (9.0, 0.0)
    assert window_hours(datetime(2026, 10, 5, 8, 0), datetime(2026, 10, 5, 9, 0), WINDOW, WEEKDAYS) == (0.0, 0.0)
    assert window_hours(_utc(2026, 10, 5, 14, 30), _utc(2026, 10, 5, 15, 30), ViewWindow(), WEEKDAYS) == (1.0, 0.0)
