"""Automatic placement after maintenance uses only slots users can see: the equipment's slot window
(``weekly_view_time_from/to``), on working days, never hidden night slots of a 24-hour equipment (Module 1)."""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment import maintenance_reschedule as mr
from iic_booking.equipment.models import Booking, BookingStatus, DailySlot, Holiday, SlotMaster, SlotStatus
from iic_booking.equipment.tests.test_disruption_log import _no_holidays, _slots, _user  # noqa: F401
from iic_booking.equipment.tests.test_maintenance_reschedule import (  # noqa: F401
    _allocate,
    _at,
    _held,
    _locmem_email,
    _past,
    _starts,
    lab,
)
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db


@pytest.fixture
def round_the_clock(lab):
    """The lab's equipment with hourly slots for all 24 hours, users seeing only 09:30-17:30."""
    eq, oic, day, now = lab
    taken = set(SlotMaster.objects.filter(equipment=eq).values_list("open_time", flat=True))
    number = SlotMaster.objects.filter(equipment=eq).count()
    for hour in range(24):
        if time(hour) in taken:
            continue
        number += 1
        close = time(hour + 1) if hour < 23 else time(23, 59)
        SlotMaster.objects.create(equipment=eq, slot_number=number, open_time=time(hour), close_time=close,
                                  is_active=True)
    eq.weekly_view_time_from, eq.weekly_view_time_to = time(9, 30), time(17, 30)
    eq.save(update_fields=["weekly_view_time_from", "weekly_view_time_to"])
    return eq, oic, day, now


def _next_working_day(d):
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_the_window_edges_decide_which_slots_are_eligible(round_the_clock):
    eq, _oic, day, now = round_the_clock
    _slots(eq, day)
    free = mr._free_slots(eq, mr.earliest_new_start(now, mr.settings_row()), day, lock=False)
    hours = sorted(timezone.localtime(s.start_datetime).hour for s in free if s.date == day)
    # 09:00 starts before 09:30 and 17:00 ends after 17:30; 10:00 and 16:00 are inside; nights never.
    assert hours == [10, 11, 12, 13, 14, 15, 16]


def test_a_hold_goes_to_the_first_visible_slot_not_to_midnight(round_the_clock, django_capture_on_commit_callbacks):
    eq, _oic, day, now = round_the_clock
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    res = _allocate(eq, now, django_capture_on_commit_callbacks)

    assert [x["booking"].pk for x in res.placed] == [b.pk]
    assert _starts(b) == [_at(day, 10)]


def test_several_holds_keep_their_order_inside_the_window(round_the_clock, django_capture_on_commit_callbacks):
    eq, _oic, day, now = round_the_clock
    first = _held(eq, _user(UserType.STUDENT), _past(5), (9,))
    second = _held(eq, _user(UserType.STUDENT), _past(2), (10,))

    _allocate(eq, now, django_capture_on_commit_callbacks)

    assert _starts(first) == [_at(day, 10)] and _starts(second) == [_at(day, 11)]


def test_a_full_visible_day_moves_to_the_next_working_day_not_into_the_evening(round_the_clock,
                                                                                 django_capture_on_commit_callbacks):
    eq, _oic, day, now = round_the_clock
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    _slots(eq, day)
    DailySlot.objects.filter(slot_master__equipment=eq, date=day).exclude(booking=b).update(status=SlotStatus.BLOCKED)
    Holiday.objects.create(date=_next_working_day(day), reason="Institute holiday", is_active=True)

    _allocate(eq, now, django_capture_on_commit_callbacks)

    assert _starts(b) == [_at(_next_working_day(_next_working_day(day)), 10)]


def test_no_eligible_slot_keeps_the_booking_on_hold(round_the_clock, django_capture_on_commit_callbacks):
    eq, _oic, _day, now = round_the_clock
    eq.weekly_view_time_from, eq.weekly_view_time_to = time(9, 30), time(10, 30)  # no whole 1-hour slot fits
    eq.save(update_fields=["weekly_view_time_from", "weekly_view_time_to"])
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    res = _allocate(eq, now, django_capture_on_commit_callbacks)

    assert [x.pk for x in res.unplaced] == [b.pk] and not res.placed
    b.refresh_from_db()
    assert b.status == BookingStatus.DISRUPTION_PENDING and b.maintenance_auto_reschedule_pending


def test_allocation_is_idempotent(round_the_clock, django_capture_on_commit_callbacks):
    eq, _oic, day, now = round_the_clock
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    _allocate(eq, now, django_capture_on_commit_callbacks)
    again = _allocate(eq, now, django_capture_on_commit_callbacks)
    assert not again.placed and not again.unplaced
    assert _starts(b) == [_at(day, 10)]
    assert Booking.objects.get(pk=b.pk).daily_slots.count() == 1


def test_the_preview_uses_the_same_window(round_the_clock):
    eq, _oic, day, now = round_the_clock
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    _slots(eq, day)
    preview = mr.allocation_preview(eq, now=now)
    assert [(r["booking_id"], r["new_start"]) for r in preview["rows"]] == [(b.pk, mr._iso(_at(day, 10)))]


# --- the shared rule (booking page, booking, alternatives, waitlist, maintenance placement) ---------------------------

def _slot(day, start_h, start_m, end_h, end_m, *, next_day=False):
    from types import SimpleNamespace

    end_day = day + timedelta(days=1) if next_day else day
    return SimpleNamespace(start_datetime=_at(day, 0).replace(hour=start_h, minute=start_m),
                           end_datetime=_at(end_day, 0).replace(hour=end_h, minute=end_m))


@pytest.mark.parametrize("slot_args,inside", [
    ((10, 0, 11, 0), True),
    ((16, 0, 17, 0), True),
    ((16, 30, 17, 30), True),   # ends exactly at "time to"
    ((9, 30, 10, 30), True),    # starts exactly at "time from"
    ((9, 0, 10, 0), False),
    ((17, 0, 18, 0), False),
    ((19, 0, 20, 0), False),
    ((2, 0, 3, 0), False),
])
def test_slot_window_rule(slot_args, inside):
    from types import SimpleNamespace

    from iic_booking.equipment.slot_window import slot_in_window

    eq = SimpleNamespace(weekly_view_time_from=time(9, 30), weekly_view_time_to=time(17, 30))
    assert slot_in_window(eq, _slot(_past(2), *slot_args)) is inside


def test_a_slot_running_past_midnight_is_outside_any_time_to():
    from types import SimpleNamespace

    from iic_booking.equipment.slot_window import slot_in_window

    late = _slot(_past(2), 23, 0, 0, 0, next_day=True)  # ends 00:00, which reads as earlier than 17:30
    assert slot_in_window(SimpleNamespace(weekly_view_time_from=time(9, 30), weekly_view_time_to=time(17, 30)),
                          late) is False
    assert slot_in_window(SimpleNamespace(weekly_view_time_from=None, weekly_view_time_to=None), late) is True
    assert slot_in_window(SimpleNamespace(weekly_view_time_from=time(20), weekly_view_time_to=None), late) is True
