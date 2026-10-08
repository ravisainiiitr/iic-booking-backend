"""Availability summary of multi-mode equipment: weekday patterns, day statuses, booking window, visibility."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from iic_booking.equipment import mode_availability as ma
from iic_booking.equipment.models import (
    DailySlot,
    EquipmentModeSchedule,
    Holiday,
    ModeAvailability,
    ModeScheduleBehavior,
    SlotMaster,
)
from iic_booking.equipment.template_slot_preference import booking_window as user_booking_window
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

MONDAY = date(2030, 1, 7)
TUESDAY_10AM = timezone.make_aware(datetime(2030, 1, 8, 10, 0), timezone.get_current_timezone())
INTERNAL = ma.Viewer(ma.VIEWER_INTERNAL)
EXTERNAL = ma.Viewer(ma.VIEWER_EXTERNAL, signed_in=True)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _slot(eq, day: date, hour: int, status="AVAILABLE", **kw):
    master, _ = SlotMaster.objects.get_or_create(
        equipment=eq, slot_number=hour, defaults={"open_time": time(hour), "close_time": time(hour + 1), "is_active": True}
    )
    start = timezone.make_aware(datetime.combine(day, time(hour)), timezone.get_current_timezone())
    return DailySlot.objects.create(
        slot_master=master, date=day, start_datetime=start, end_datetime=start + timedelta(hours=1), status=status, **kw
    )


def _schedule(base, mode, *, weekdays=None, behavior=ModeScheduleBehavior.PARALLEL, **kw):
    return EquipmentModeSchedule.objects.create(
        parent_equipment=base, mode_equipment=mode, weekdays=weekdays or [], behavior=behavior, **kw
    )


@pytest.fixture
def family(egs_factory):
    """Base runs Mon–Thu; Depth only Tue/Thu; UPS every weekday and on its own on Fridays."""
    f = egs_factory
    base = f.equipment(name="XPS", code="MMA-BASE", enable_multi_mode=True)
    depth = f.equipment(name="XPS Depth", code="MMA-DEPTH", parent_equipment=base,
                        mode_availability=ModeAvailability.SCHEDULED_ONLY)
    ups = f.equipment(name="XPS UPS", code="MMA-UPS", parent_equipment=base)
    _schedule(base, depth, weekdays=[1, 3])
    _schedule(base, ups, weekdays=[4], behavior=ModeScheduleBehavior.EXCLUSIVE,
              exclusive_blocked_label="UPS running")
    return base, depth, ups


def _summary(base, viewer=INTERNAL, now=TUESDAY_10AM):
    return ma.family_summaries([base.pk], viewer, now=now)[base.pk]


def _mode(summary, eq):
    return next(m for m in summary["modes"] if m["equipment_id"] == eq.pk)


def _cell(summary, eq, day: date):
    row = next(d for d in summary["days"] if d["date"] == day.isoformat())
    return next(c for c in row["modes"] if c["equipment_id"] == eq.pk)


def test_weekday_patterns_follow_mode_schedules(family):
    base, depth, ups = family
    s = _summary(base)
    assert [m["equipment_id"] for m in s["modes"]] == [base.pk, depth.pk, ups.pk]
    assert _mode(s, base)["weekdays"] == [0, 1, 2, 3]
    assert _mode(s, depth)["weekdays"] == [1, 3]
    assert _mode(s, ups)["weekdays"] == [0, 1, 2, 3, 4]
    assert _mode(s, base)["role"] == "base" and _mode(s, depth)["role"] == "mode"
    assert s["start_date"] == MONDAY.isoformat() and len(s["days"]) == 28


def test_open_weekend_counts_in_pattern(family):
    base, _depth, ups = family
    _slot(ups, MONDAY + timedelta(days=5), 10)
    assert _mode(_summary(base), ups)["weekdays"] == [0, 1, 2, 3, 4, 5]


def test_day_statuses_inside_the_window(family):
    base, depth, ups = family
    wed, thu, fri = MONDAY + timedelta(days=2), MONDAY + timedelta(days=3), MONDAY + timedelta(days=4)
    _slot(base, wed, 9)
    _slot(base, wed, 11)
    _slot(depth, thu, 10, status="BOOKED")
    _slot(base, TUESDAY_10AM.date(), 9)
    s = _summary(base)
    assert _cell(s, base, MONDAY)["status"] == ma.PAST
    wed_cell = _cell(s, base, wed)
    assert (wed_cell["status"], wed_cell["free_slots"], wed_cell["label"]) == (ma.AVAILABLE, 2, "2 free slots")
    assert _cell(s, depth, thu)["status"] == ma.FULL
    assert _cell(s, ups, wed) == {"status": ma.NOT_AVAILABLE, "label": "No slots", "equipment_id": ups.pk}
    assert _cell(s, base, TUESDAY_10AM.date())["label"] == "No more slots today"
    blocked = _cell(s, base, fri)
    assert blocked["status"] == ma.NOT_RUNNING and blocked["label"] == "UPS running" and blocked["blocked_by"] == ups.pk
    assert _cell(s, depth, wed)["status"] == ma.NOT_RUNNING
    assert _cell(s, ups, MONDAY + timedelta(days=5))["status"] == ma.CLOSED
    assert _mode(s, base)["next_available"]["date"] == wed.isoformat()
    assert _mode(s, base)["state"] == ma.AVAILABLE
    assert _mode(s, depth)["state"] == ma.FULL


def test_time_window_exclusive_only_blocks_matching_slots(family):
    base, _depth, ups = family
    wed = MONDAY + timedelta(days=2)
    _schedule(base, ups, weekdays=[2], behavior=ModeScheduleBehavior.EXCLUSIVE, start_time=time(9), end_time=time(12))
    _slot(base, wed, 10)
    _slot(base, wed, 14)
    cell = _cell(_summary(base), base, wed)
    assert cell["status"] == ma.AVAILABLE and cell["free_slots"] == 1 and cell["partial"] is True


def test_days_after_the_window_show_when_booking_opens(family):
    base, depth, _ups = family
    for eq in family:
        eq.slot_window_reference_weekday = 2
        eq.slot_window_reference_time = time(21)
        eq.save(update_fields=["slot_window_reference_weekday", "slot_window_reference_time"])
    next_tue = MONDAY + timedelta(days=8)
    _slot(depth, next_tue, 10)  # already generated, but the week is not open yet
    s = _summary(base)
    cell = _cell(s, depth, next_tue)
    assert cell["status"] == ma.NOT_OPEN and "free_slots" not in cell
    tz = timezone.get_current_timezone()
    assert cell["opens_at"] == timezone.make_aware(datetime(2030, 1, 9, 21), tz).isoformat()
    week3 = _cell(s, depth, MONDAY + timedelta(days=15))
    assert week3["opens_at"] == timezone.make_aware(datetime(2030, 1, 16, 21), tz).isoformat()
    assert _mode(s, depth)["next_opening"]["date"] == next_tue.isoformat()
    assert _mode(s, depth)["next_available"] is None
    assert _cell(s, depth, MONDAY + timedelta(days=13))["status"] == ma.NOT_RUNNING

    after = timezone.make_aware(datetime(2030, 1, 9, 21, 5), tz)
    cache.clear()
    opened = _cell(_summary(base, now=after), depth, next_tue)
    assert opened["status"] == ma.AVAILABLE and opened["free_slots"] == 1


def test_external_window_starts_next_week(family):
    base, depth, _ups = family
    thu, next_tue = MONDAY + timedelta(days=3), MONDAY + timedelta(days=8)
    _slot(depth, thu, 10)
    _slot(depth, next_tue, 10)
    s = _summary(base, viewer=EXTERNAL)
    assert _cell(s, depth, thu)["label"] == "Outside your booking window"
    assert _cell(s, depth, next_tue)["status"] == ma.AVAILABLE
    assert _mode(s, depth)["next_available"]["date"] == next_tue.isoformat()


def test_holidays_and_maintenance(family):
    base, depth, _ups = family
    thu = MONDAY + timedelta(days=3)
    Holiday.objects.create(date=thu, reason="Founders Day", is_active=True)
    Holiday.objects.create(date=MONDAY + timedelta(days=24), reason="Republic Day", is_active=True)
    s = _summary(base)
    assert _cell(s, depth, thu) == {"status": ma.HOLIDAY, "label": "Founders Day", "equipment_id": depth.pk}
    assert _cell(s, base, MONDAY + timedelta(days=24))["status"] == ma.HOLIDAY
    assert next(d for d in s["days"] if d["date"] == thu.isoformat())["holiday"] == "Founders Day"

    base.status = "REPAIR"
    base.save(update_fields=["status"])
    cache.clear()
    s = _summary(base)
    assert _cell(s, base, MONDAY + timedelta(days=2))["status"] == ma.MAINTENANCE
    assert _mode(s, base)["state"] == ma.MAINTENANCE and _mode(s, base)["operational"] is False


def test_home_department_split_for_signed_in_users(family, egs_factory):
    base, _depth, _ups = family
    wed = MONDAY + timedelta(days=9)
    _slot(base, wed, 10, home_department_only=True)
    home = ma.Viewer(ma.VIEWER_INTERNAL, egs_factory.department.pk, True)
    other = ma.Viewer(ma.VIEWER_INTERNAL, -1, True)
    assert _cell(_summary(base, viewer=home), base, wed)["label"] == "Reserved for another department"
    assert _cell(_summary(base, viewer=other), base, wed)["status"] == ma.AVAILABLE


@pytest.mark.parametrize("reference", [None, (2, time(21))])
@pytest.mark.parametrize("user_type", [UserType.STUDENT, UserType.EXTERNAL, UserType.ADMIN])
@pytest.mark.parametrize("hour", [(1, 10), (2, 20), (2, 22), (6, 23)])
def test_window_matches_booking_window(egs_factory, reference, user_type, hour):
    eq = egs_factory.equipment()
    if reference:
        eq.slot_window_reference_weekday, eq.slot_window_reference_time = reference
        eq.save()
    user = UserFactory(user_type=user_type, department=egs_factory.department)
    now = timezone.make_aware(datetime.combine(MONDAY + timedelta(days=hour[0]), time(hour[1])),
                              timezone.get_current_timezone())
    mine = ma.booking_window(eq, ma.viewer_for(user), None, now)
    theirs = user_booking_window(eq, user, now=now)
    assert (mine.min_date, mine.max_date) == (theirs.min_date, theirs.max_date)


def test_not_multi_mode(egs_factory):
    eq = egs_factory.equipment()
    assert ma.family_summaries([eq.pk], INTERNAL) == {}
    res = egs_factory.client_for(egs_factory.student()).get(f"/api/equipments/{eq.pk}/mode-availability/")
    assert res.status_code == 200 and res.data["multi_mode"] is False


def test_endpoint_respects_visibility(family, egs_factory):
    from rest_framework.test import APIClient

    from iic_booking.users.models.user_group import UserGroup

    base, depth, ups = family
    group = UserGroup.objects.create(name="MMA private", code="MMAPRIV")
    ups.visibility_group = group
    ups.save(update_fields=["visibility_group"])
    anon = APIClient()
    res = anon.get(f"/api/equipments/{depth.pk}/mode-availability/")
    assert res.status_code == 200 and res.data["multi_mode"] is True
    ids = [m["equipment_id"] for m in res.data["modes"]]
    assert ids == [base.pk, depth.pk]
    assert all(len(d["modes"]) == 2 for d in res.data["days"])
    assert anon.get(f"/api/equipments/{ups.pk}/mode-availability/").status_code == 404
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    res = egs_factory.client_for(admin).get(f"/api/equipments/{base.pk}/mode-availability/")
    assert [m["equipment_id"] for m in res.data["modes"]] == [base.pk, depth.pk, ups.pk]


def test_catalog_cards_get_compact_availability(family, egs_factory):
    base, depth, ups = family
    plain = egs_factory.equipment(name="Plain")
    res = egs_factory.client_for(egs_factory.student()).get("/api/equipments/")
    rows = {r["equipment_id"]: r for r in res.data["equipments"]}
    assert "mode_availability" not in rows[plain.pk]
    parent_modes = rows[base.pk]["mode_availability"]["modes"]
    assert [m["equipment_id"] for m in parent_modes] == [base.pk, depth.pk, ups.pk]
    assert set(parent_modes[0]) == set(ma._CARD_FIELDS)
    assert "days" not in rows[base.pk]["mode_availability"]
    child = rows[depth.pk]["mode_availability"]
    assert child["parent_equipment_id"] == base.pk
    assert [m["equipment_id"] for m in child["modes"]] == [depth.pk]
    assert child["modes"][0]["weekdays"] == [1, 3]


def test_summary_query_count_does_not_grow_with_families(egs_factory):
    def make_family(i):
        base = egs_factory.equipment(code=f"MMQ-B{i}", enable_multi_mode=True)
        mode = egs_factory.equipment(code=f"MMQ-M{i}", parent_equipment=base)
        _schedule(base, mode, weekdays=[1])
        for d in range(5):
            _slot(base, MONDAY + timedelta(days=d), 10)
            _slot(mode, MONDAY + timedelta(days=d), 10)
        return base

    bases = [make_family(i) for i in range(4)]
    with CaptureQueriesContext(connection) as one:
        ma.family_summaries([bases[0].pk], INTERNAL, now=TUESDAY_10AM)
    cache.clear()
    with CaptureQueriesContext(connection) as many:
        ma.family_summaries([b.pk for b in bases], INTERNAL, now=TUESDAY_10AM)
    assert len(many.captured_queries) == len(one.captured_queries) <= 6
    with CaptureQueriesContext(connection) as warm:
        ma.family_summaries([b.pk for b in bases], INTERNAL, now=TUESDAY_10AM)
    assert len(warm.captured_queries) == 0


def test_catalog_query_budget_with_families(family, egs_factory):
    client = egs_factory.client_for(egs_factory.student())
    client.get("/api/equipments/", {"include_ratings": "1"})
    with CaptureQueriesContext(connection) as ctx:
        res = client.get("/api/equipments/", {"include_ratings": "1"})
    assert res.status_code == 200
    assert len(ctx.captured_queries) <= 8


def test_endpoint_query_budget(family, egs_factory):
    base, depth, _ups = family
    client = egs_factory.client_for(egs_factory.student())
    with CaptureQueriesContext(connection) as ctx:
        res = client.get(f"/api/equipments/{depth.pk}/mode-availability/")
    assert res.status_code == 200
    assert len(ctx.captured_queries) <= 20
