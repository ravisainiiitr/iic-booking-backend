"""Blank schedule dates mean "always available"; schedules define mode membership; staff see user blocks."""

from __future__ import annotations

import importlib
from datetime import date, datetime, time, timedelta

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.utils import timezone

from iic_booking.equipment import mode_utils
from iic_booking.equipment.models import (
    Equipment,
    EquipmentModeAuditLog,
    EquipmentModeSchedule,
    ModeAvailability,
    ModeScheduleBehavior,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

MONDAY = date(2030, 1, 7)
SCHEDULES_URL = "/api/oic/multi-mode/schedules/"


def _schedule(base, mode, start=None, end=None, *, behavior=ModeScheduleBehavior.PARALLEL, weekdays=None, **kw):
    return EquipmentModeSchedule.objects.create(
        parent_equipment=base, mode_equipment=mode, start_date=start, end_date=end,
        behavior=behavior, weekdays=weekdays or [], **kw,
    )


@pytest.fixture
def family(egs_factory):
    f = egs_factory
    base = f.equipment(name="XPS base", enable_multi_mode=True)
    depth = f.equipment(name="Depth", parent_equipment=base, mode_availability=ModeAvailability.SCHEDULED_ONLY)
    ups = f.equipment(name="UPS", parent_equipment=base, mode_availability=ModeAvailability.SCHEDULED_ONLY)
    return base, depth, ups


def _admin_client(f):
    return f.client_for(UserFactory(user_type=UserType.ADMIN, admin_approved=True))


def _bookable(eq, day, hour=10):
    eq.refresh_from_db()
    return mode_utils.equipment_bookable_on_date(eq, day, time(hour))[0]


# --- rule: blank dates = always, dates = only scheduled days ---------------------------------------------------------


def test_blank_dates_mean_always_available(family):
    base, depth, _ = family
    assert not _bookable(depth, MONDAY)
    _schedule(base, depth)
    for day in (date(2020, 1, 1), MONDAY, MONDAY + timedelta(days=3), date(2099, 12, 31)):
        assert _bookable(depth, day)
    assert mode_utils.schedules_covering_date(base.pk, date(2099, 12, 31))


def test_blank_dates_still_respect_weekdays_and_hours(family):
    base, depth, _ = family
    _schedule(base, depth, weekdays=[0], start_time=time(9), end_time=time(12))
    assert _bookable(depth, MONDAY, 10)
    assert not _bookable(depth, MONDAY, 15)
    assert not _bookable(depth, MONDAY + timedelta(days=1), 10)
    assert _bookable(depth, MONDAY + timedelta(days=700), 10)


def test_dates_mean_only_scheduled_days(family):
    base, depth, _ = family
    _schedule(base, depth, MONDAY, MONDAY + timedelta(days=4))
    assert _bookable(depth, MONDAY)
    assert _bookable(depth, MONDAY + timedelta(days=4))
    assert not _bookable(depth, MONDAY + timedelta(days=5))
    assert not _bookable(depth, MONDAY - timedelta(days=1))


def test_blank_dated_exclusive_blocks_base_and_siblings_every_day(family):
    base, depth, ups = family
    _schedule(base, ups)
    _schedule(base, depth, behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[2])
    wednesday = MONDAY + timedelta(days=2 + 70)
    assert not _bookable(base, wednesday)
    assert not _bookable(ups, wednesday)
    assert _bookable(base, wednesday + timedelta(days=1))
    assert mode_utils.is_equipment_visible_on_date(base, wednesday) is False


def test_validation_needs_both_dates_or_neither(family):
    base, depth, _ = family
    for start, end in ((MONDAY, None), (None, MONDAY)):
        with pytest.raises(ValidationError):
            EquipmentModeSchedule(parent_equipment=base, mode_equipment=depth, start_date=start, end_date=end).full_clean()
    EquipmentModeSchedule(parent_equipment=base, mode_equipment=depth).full_clean()


def test_blank_dated_exclusive_overlaps_any_exclusive_unless_weekdays_differ(family):
    base, depth, ups = family
    _schedule(base, depth, MONDAY, MONDAY + timedelta(days=30), behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[0])
    clash = EquipmentModeSchedule(parent_equipment=base, mode_equipment=ups, behavior=ModeScheduleBehavior.EXCLUSIVE)
    with pytest.raises(ValidationError):
        clash.full_clean()
    EquipmentModeSchedule(
        parent_equipment=base, mode_equipment=ups, behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[3]
    ).full_clean()


# --- API: optional dates and membership ---------------------------------------------------------------------------------


def test_api_creates_always_schedule_with_blank_dates(family, egs_factory):
    base, depth, _ = family
    client = _admin_client(egs_factory)
    res = client.post(
        SCHEDULES_URL,
        {"parent_equipment_id": base.pk, "mode_equipment_id": depth.pk, "start_date": "", "end_date": None},
        format="json",
    )
    assert res.status_code == 201, res.data
    assert res.data["schedule"]["always"] is True
    assert res.data["schedule"]["start_date"] is None
    res = client.post(
        SCHEDULES_URL,
        {"parent_equipment_id": base.pk, "mode_equipment_id": depth.pk, "start_date": "2030-01-07", "end_date": ""},
        format="json",
    )
    assert res.status_code == 400
    sid = EquipmentModeSchedule.objects.get(mode_equipment=depth).pk
    res = client.patch(f"{SCHEDULES_URL}{sid}/", {"start_date": "2030-01-07", "end_date": "2030-01-09"}, format="json")
    assert res.status_code == 200 and res.data["schedule"]["always"] is False
    res = client.patch(f"{SCHEDULES_URL}{sid}/", {"start_date": None, "end_date": ""}, format="json")
    assert res.status_code == 200 and res.data["schedule"]["always"] is True


def test_schedule_for_eligible_equipment_makes_it_a_mode(family, egs_factory):
    f = egs_factory
    base, _, _ = family
    loose = f.equipment(name="Loose")
    client = _admin_client(f)
    res = client.post(
        SCHEDULES_URL,
        {"parent_equipment_id": base.pk, "mode_equipment_id": loose.pk, "start_date": "2030-01-07", "end_date": "2030-01-08"},
        format="json",
    )
    assert res.status_code == 201, res.data
    assert res.data["mode_linked"] is True
    loose.refresh_from_db()
    assert loose.parent_equipment_id == base.pk
    assert loose.mode_availability == ModeAvailability.SCHEDULED_ONLY
    assert EquipmentModeAuditLog.objects.filter(equipment=loose, action="MODE_LINKED").exists()
    assert _bookable(loose, MONDAY) and not _bookable(loose, MONDAY + timedelta(days=2))


def test_new_base_gets_flagged_when_first_mode_is_scheduled(egs_factory):
    f = egs_factory
    base = f.equipment(name="Fresh base")
    mode = f.equipment(name="Fresh mode")
    res = _admin_client(f).post(
        SCHEDULES_URL, {"parent_equipment_id": base.pk, "mode_equipment_id": mode.pk}, format="json"
    )
    assert res.status_code == 201, res.data
    base.refresh_from_db()
    assert base.enable_multi_mode is True
    assert _bookable(mode, date(2031, 5, 5))


def test_ineligible_or_invalid_schedule_does_not_link(family, egs_factory):
    f = egs_factory
    base, _, _ = family
    other_dept = Department.objects.create(name="Elsewhere", code="ELSX")
    foreign = f.equipment(name="Foreign", internal_department=other_dept)
    loose = f.equipment(name="Loose")
    client = _admin_client(f)
    res = client.post(SCHEDULES_URL, {"parent_equipment_id": base.pk, "mode_equipment_id": foreign.pk}, format="json")
    assert res.status_code == 400
    res = client.post(
        SCHEDULES_URL,
        {"parent_equipment_id": base.pk, "mode_equipment_id": loose.pk, "start_date": "2030-01-09", "end_date": "2030-01-07"},
        format="json",
    )
    assert res.status_code == 400
    foreign.refresh_from_db()
    loose.refresh_from_db()
    assert foreign.parent_equipment_id is None and loose.parent_equipment_id is None


def test_deleting_last_schedule_unlinks_mode_unless_it_has_bookings(family, egs_factory):
    f = egs_factory
    base, depth, ups = family
    client = _admin_client(f)
    first = _schedule(base, depth)
    second = _schedule(base, depth, MONDAY, MONDAY)
    res = client.delete(f"{SCHEDULES_URL}{first.pk}/")
    assert res.status_code == 200 and res.data["mode_unlinked"] is False
    res = client.delete(f"{SCHEDULES_URL}{second.pk}/")
    assert res.data["mode_unlinked"] is True
    depth.refresh_from_db()
    assert depth.parent_equipment_id is None
    base.refresh_from_db()
    assert base.enable_multi_mode is True

    f.booking(f.student(), ups, f.future(days=4))
    only = _schedule(base, ups)
    res = client.delete(f"{SCHEDULES_URL}{only.pk}/")
    assert res.data["mode_unlinked"] is False
    ups.refresh_from_db()
    assert ups.parent_equipment_id == base.pk


def test_moving_a_schedule_to_another_mode_links_new_and_unlinks_old(family, egs_factory):
    f = egs_factory
    base, depth, _ = family
    loose = f.equipment(name="Loose")
    sched = _schedule(base, depth, MONDAY, MONDAY + timedelta(days=1))
    res = _admin_client(f).patch(f"{SCHEDULES_URL}{sched.pk}/", {"mode_equipment_id": loose.pk}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["mode_linked"] is True and res.data["mode_unlinked"] is True
    depth.refresh_from_db()
    loose.refresh_from_db()
    assert depth.parent_equipment_id is None and loose.parent_equipment_id == base.pk


def test_remove_mode_deletes_current_schedules_keeps_past_and_refuses_with_bookings(family, egs_factory):
    f = egs_factory
    base, depth, ups = family
    client = _admin_client(f)
    today = timezone.localdate()
    past = _schedule(base, depth, today - timedelta(days=30), today - timedelta(days=20))
    _schedule(base, depth)
    _schedule(base, depth, today, today + timedelta(days=3))
    res = client.delete(f"/api/oic/multi-mode/families/{base.pk}/modes/{depth.pk}/")
    assert res.status_code == 200, res.data
    assert res.data["schedules_deleted"] == 2
    assert list(EquipmentModeSchedule.objects.filter(mode_equipment=depth).values_list("pk", flat=True)) == [past.pk]
    depth.refresh_from_db()
    assert depth.parent_equipment_id is None
    assert EquipmentModeAuditLog.objects.filter(equipment=depth, action="MODE_UNLINKED").exists()

    booking = f.booking(f.student(), ups, f.future(days=4))
    res = client.delete(f"/api/oic/multi-mode/families/{base.pk}/modes/{ups.pk}/")
    assert res.status_code == 409
    assert booking.virtual_booking_id in res.data["error"]
    ups.refresh_from_db()
    assert ups.parent_equipment_id == base.pk


def test_family_listing_reports_current_schedule_count_and_open_schedules_first(family, egs_factory):
    f = egs_factory
    base, depth, ups = family
    _schedule(base, depth, MONDAY, MONDAY)
    _schedule(base, depth)
    data = _admin_client(f).get(f"/api/oic/multi-mode/families/{base.pk}/").data
    counts = {c["equipment_id"]: c["current_schedule_count"] for c in data["family"]["children"]}
    assert counts == {depth.pk: 2, ups.pk: 0}
    assert data["family"]["schedules"][0]["always"] is True


# --- staff see slots users cannot book --------------------------------------------------------------------------------


def test_staff_rows_keep_status_but_flag_user_blocks(family, egs_factory):
    f = egs_factory
    base, depth, ups = family
    _schedule(base, ups, behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[0])
    start = timezone.make_aware(datetime.combine(MONDAY, time(10)))
    tuesday = start + timedelta(days=1)
    slots = [f.slot(depth, start), f.slot(base, start), f.slot(ups, start), f.slot(base, tuesday)]

    def rows(eq):
        own = [s for s in slots if s.slot_master.equipment_id == eq.pk]
        return mode_utils.annotate_user_mode_blocks_for_staff(eq, own, [{"id": s.id, "status": "AVAILABLE"} for s in own])

    depth_row = rows(depth)[0]
    assert depth_row["status"] == "AVAILABLE"
    assert depth_row["users_blocked_label"] == "Mode not scheduled"
    assert "No schedule" in depth_row["users_blocked_reason"]
    base_rows = rows(base)
    assert "runs on its own" in base_rows[0]["users_blocked_reason"]
    assert "users_blocked_reason" not in base_rows[1]
    assert "users_blocked_reason" not in rows(ups)[0]

    user_rows = mode_utils.apply_mode_overlays_to_slot_payloads(depth, [slots[0]], [{"id": slots[0].id, "status": "AVAILABLE"}])
    assert user_rows[0]["status"] == "BLOCKED" and user_rows[0]["mode_overlay"] == "child_unavailable"


# --- data migration -----------------------------------------------------------------------------------------------------


def test_migration_turns_always_modes_into_open_schedules_with_same_bookability(egs_factory):
    f = egs_factory
    base = f.equipment(name="NMR", enable_multi_mode=True)
    always = f.equipment(name="Always mode", parent_equipment=base, mode_availability=ModeAvailability.ALWAYS)
    scheduled = f.equipment(name="Scheduled mode", parent_equipment=base, mode_availability=ModeAvailability.SCHEDULED_ONLY)
    excl = f.equipment(name="Exclusive mode", parent_equipment=base, mode_availability=ModeAvailability.SCHEDULED_ONLY)
    _schedule(base, scheduled, MONDAY, MONDAY + timedelta(days=3))
    _schedule(base, excl, MONDAY + timedelta(days=5), MONDAY + timedelta(days=6), behavior=ModeScheduleBehavior.EXCLUSIVE,
              start_time=time(9), end_time=time(12))
    _schedule(base, always, MONDAY + timedelta(days=1), MONDAY + timedelta(days=1))
    members = (base, always, scheduled, excl)
    samples = [(MONDAY + timedelta(days=d), h) for d in range(-2, 10) for h in (10, 15)]

    def snapshot():
        return {(eq.pk, day, h): _bookable(eq, day, h) for eq in members for day, h in samples}

    before = snapshot()
    assert before[(always.pk, MONDAY + timedelta(days=5), 10)] is False
    assert before[(always.pk, MONDAY + timedelta(days=5), 15)] is True
    count_before = EquipmentModeSchedule.objects.count()

    migration = importlib.import_module("iic_booking.equipment.migrations.0232_mode_schedule_optional_dates")
    migration.forward(django_apps, None)

    always.refresh_from_db()
    assert always.mode_availability == ModeAvailability.SCHEDULED_ONLY
    added = EquipmentModeSchedule.objects.get(mode_equipment=always, start_date__isnull=True)
    assert added.end_date is None and added.behavior == ModeScheduleBehavior.PARALLEL and added.weekdays == []
    assert EquipmentModeSchedule.objects.count() == count_before + 1
    assert snapshot() == before

    migration.forward(django_apps, None)
    assert EquipmentModeSchedule.objects.count() == count_before + 1

    migration.backward(django_apps, None)
    always.refresh_from_db()
    assert always.mode_availability == ModeAvailability.ALWAYS
    assert EquipmentModeSchedule.objects.count() == count_before
    assert not EquipmentModeAuditLog.objects.filter(action=migration.ACTION).exists()
    assert snapshot() == before


def test_migration_leaves_scheduled_only_and_unscheduled_modes_alone(egs_factory):
    f = egs_factory
    base = f.equipment(name="APREO", enable_multi_mode=True)
    ebsd = f.equipment(name="EBSD", parent_equipment=base, mode_availability=ModeAvailability.SCHEDULED_ONLY)
    plain = f.equipment(name="Plain")
    migration = importlib.import_module("iic_booking.equipment.migrations.0232_mode_schedule_optional_dates")
    migration.forward(django_apps, None)
    assert not EquipmentModeSchedule.objects.filter(mode_equipment__in=[ebsd, plain]).exists()
    assert not _bookable(ebsd, MONDAY)
    assert _bookable(plain, MONDAY)
