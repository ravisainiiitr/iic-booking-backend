"""Slot window: 00:00-23:59 is the whole day (24-hour and overnight slots visible); 23:59 also means midnight."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from types import SimpleNamespace

from django.utils import timezone

from iic_booking.equipment.slot_window import has_window, slot_in_window, window


def _eq(f, t):
    return SimpleNamespace(weekly_view_time_from=f, weekly_view_time_to=t)


def _slot(start: datetime, minutes: int):
    tz = timezone.get_current_timezone()
    s = timezone.make_aware(start, tz)
    return SimpleNamespace(start_datetime=s, end_datetime=s + timedelta(minutes=minutes))


DAY = datetime(2026, 11, 3)


def test_whole_day_window_is_no_limit_for_24_hour_and_overnight_slots():
    full = _eq(time(0, 0), time(23, 59))
    assert window(full) == (None, None) and not has_window(full)
    assert slot_in_window(full, _slot(DAY, 1440))  # TXI Probe: 00:00 to 00:00 next day
    assert slot_in_window(full, _slot(DAY.replace(hour=18), 15 * 60))  # overnight 18:00 to 09:00
    assert slot_in_window(_eq(None, time(23, 59)), _slot(DAY, 1440))
    assert slot_in_window(_eq(time(0, 0), None), _slot(DAY, 1440))


def test_window_ending_at_2359_takes_a_slot_ending_at_midnight():
    evening = _eq(time(9, 0), time(23, 59))
    assert slot_in_window(evening, _slot(DAY.replace(hour=23), 60))  # 23:00 to 00:00
    assert not slot_in_window(evening, _slot(DAY.replace(hour=23), 120))  # 23:00 to 01:00
    assert not slot_in_window(evening, _slot(DAY.replace(hour=8), 60))  # starts before 09:00


def test_a_real_window_still_limits():
    office = _eq(time(9, 0), time(18, 0))
    assert slot_in_window(office, _slot(DAY.replace(hour=9), 60))
    assert slot_in_window(office, _slot(DAY.replace(hour=17), 60))  # ends exactly 18:00
    assert not slot_in_window(office, _slot(DAY.replace(hour=17, minute=30), 60))
    assert not slot_in_window(office, _slot(DAY.replace(hour=23), 60))  # ends at midnight: after 18:00
    assert not slot_in_window(office, _slot(DAY, 1440))
    assert has_window(office)
