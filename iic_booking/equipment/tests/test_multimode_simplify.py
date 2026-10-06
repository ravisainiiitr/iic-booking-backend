"""Simplified multi-mode: family setup, availability default, weekly repeat, exclusive blocking, migration."""

from __future__ import annotations

import importlib
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.utils import timezone

from iic_booking.equipment import mode_utils
from iic_booking.equipment.models import (
    Equipment,
    EquipmentManager,
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
assert MONDAY.weekday() == 0


def _schedule(base, mode, start, end, *, behavior=ModeScheduleBehavior.PARALLEL, weekdays=None, **kw):
    return EquipmentModeSchedule.objects.create(
        parent_equipment=base,
        mode_equipment=mode,
        start_date=start,
        end_date=end,
        behavior=behavior,
        weekdays=weekdays or [],
        **kw,
    )


@pytest.fixture
def family(egs_factory):
    f = egs_factory
    base = f.equipment(name="XPS base", enable_multi_mode=True)
    depth = f.equipment(name="Depth Profile", parent_equipment=base)
    ups = f.equipment(name="UPS", parent_equipment=base)
    return base, depth, ups


def _admin():
    return UserFactory(user_type=UserType.ADMIN, admin_approved=True)


def _oic(f, *equipment):
    user = UserFactory(user_type=UserType.MANAGER, admin_approved=True, department=f.department)
    for eq in equipment:
        EquipmentManager.objects.create(equipment=eq, manager=user)
    return user


# --- weekly repeat ----------------------------------------------------------------------------------------------


def test_weekly_repeat_covers_only_selected_weekdays(family):
    base, depth, _ = family
    sched = _schedule(base, depth, MONDAY, MONDAY + timedelta(days=13), weekdays=[0, 3])
    covered = [d for d in range(14) if mode_utils.schedule_covers_date(sched, MONDAY + timedelta(days=d))]
    assert covered == [0, 3, 7, 10]
    assert not mode_utils.schedule_covers_date(sched, MONDAY - timedelta(days=7))
    assert [s.id for s in mode_utils.schedules_covering_date(base.pk, MONDAY + timedelta(days=3))] == [sched.id]
    assert mode_utils.schedules_covering_date(base.pk, MONDAY + timedelta(days=1)) == []


def test_empty_weekdays_means_every_day_and_time_window_still_applies(family):
    base, depth, _ = family
    sched = _schedule(base, depth, MONDAY, MONDAY + timedelta(days=6), start_time=time(9), end_time=time(13))
    for d in range(7):
        assert mode_utils.schedule_covers_date(sched, MONDAY + timedelta(days=d))
    assert mode_utils.schedule_covers_datetime(sched, MONDAY, time(10))
    assert not mode_utils.schedule_covers_datetime(sched, MONDAY, time(14))


def test_weekdays_are_validated_and_normalised(family):
    base, depth, _ = family
    sched = EquipmentModeSchedule(
        parent_equipment=base, mode_equipment=depth, start_date=MONDAY, end_date=MONDAY, weekdays=[3, 0, 3]
    )
    sched.full_clean()
    assert sched.weekdays == [0, 3]
    sched.weekdays = [7]
    with pytest.raises(ValidationError):
        sched.full_clean()


def test_exclusive_overlap_allowed_on_disjoint_weekdays_only(family):
    base, depth, ups = family
    _schedule(base, depth, MONDAY, MONDAY + timedelta(days=30), behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[0])
    ok = EquipmentModeSchedule(
        parent_equipment=base, mode_equipment=ups, start_date=MONDAY, end_date=MONDAY + timedelta(days=30),
        behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[3],
    )
    ok.full_clean()
    clash = EquipmentModeSchedule(
        parent_equipment=base, mode_equipment=ups, start_date=MONDAY, end_date=MONDAY + timedelta(days=30),
        behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[0, 3],
    )
    with pytest.raises(ValidationError):
        clash.full_clean()


# --- availability default -----------------------------------------------------------------------------------------


def test_unscheduled_mode_is_bookable_by_default(family):
    base, depth, _ = family
    assert depth.mode_availability == ModeAvailability.ALWAYS
    assert mode_utils.equipment_bookable_on_date(depth, MONDAY, time(10)) == (True, "")
    assert mode_utils.equipment_bookable_on_date(base, MONDAY, time(10)) == (True, "")


def test_scheduled_only_mode_books_only_on_its_scheduled_weekdays(family):
    base, depth, _ = family
    Equipment.objects.filter(pk=depth.pk).update(mode_availability=ModeAvailability.SCHEDULED_ONLY)
    depth.refresh_from_db()
    assert mode_utils.equipment_bookable_on_date(depth, MONDAY, time(10))[0] is False
    _schedule(base, depth, MONDAY, MONDAY + timedelta(days=13), weekdays=[0])
    assert mode_utils.equipment_bookable_on_date(depth, MONDAY, time(10))[0] is True
    assert mode_utils.equipment_bookable_on_date(depth, MONDAY + timedelta(days=1), time(10))[0] is False
    assert mode_utils.equipment_bookable_on_date(depth, MONDAY + timedelta(days=7), time(10))[0] is True


def test_overlay_only_for_scheduled_only_modes_outside_schedule(family, egs_factory):
    base, depth, ups = family
    Equipment.objects.filter(pk=depth.pk).update(mode_availability=ModeAvailability.SCHEDULED_ONLY)
    depth.refresh_from_db()
    start = timezone.make_aware(datetime.combine(MONDAY, time(10)))
    depth_slot = egs_factory.slot(depth, start)
    ups_slot = egs_factory.slot(ups, start)
    assert mode_utils.slot_mode_overlay(depth, depth_slot)["mode_overlay"] == "child_unavailable"
    assert mode_utils.slot_mode_overlay(ups, ups_slot) is None


def test_non_multimode_equipment_unaffected(egs_factory):
    plain = egs_factory.equipment(name="Plain SEM")
    assert mode_utils.equipment_bookable_on_date(plain, MONDAY, time(10)) == (True, "")
    assert mode_utils.is_equipment_visible_on_date(plain, MONDAY) is True


# --- exclusive blocking ---------------------------------------------------------------------------------------------


def test_exclusive_blocks_base_and_siblings_on_its_weekdays_only(family, egs_factory):
    base, depth, ups = family
    _schedule(base, depth, MONDAY, MONDAY + timedelta(days=13), behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[0])
    assert mode_utils.equipment_bookable_on_date(depth, MONDAY, time(10))[0] is True
    assert mode_utils.equipment_bookable_on_date(base, MONDAY, time(10))[0] is False
    assert mode_utils.equipment_bookable_on_date(ups, MONDAY, time(10))[0] is False
    tuesday = MONDAY + timedelta(days=1)
    assert mode_utils.equipment_bookable_on_date(base, tuesday, time(10))[0] is True
    assert mode_utils.equipment_bookable_on_date(ups, tuesday, time(10))[0] is True
    assert mode_utils.is_equipment_visible_on_date(base, MONDAY) is False
    assert mode_utils.is_equipment_visible_on_date(base, tuesday) is True

    start = timezone.make_aware(datetime.combine(MONDAY, time(10)))
    assert mode_utils.slot_mode_overlay(base, egs_factory.slot(base, start))["mode_overlay"] == "exclusive_parent"
    assert mode_utils.slot_mode_overlay(ups, egs_factory.slot(ups, start))["mode_overlay"] == "exclusive_sibling"


def test_catalog_hides_base_only_on_exclusive_weekday(family):
    base, depth, _ = family
    _schedule(base, depth, MONDAY, MONDAY + timedelta(days=13), behavior=ModeScheduleBehavior.EXCLUSIVE, weekdays=[0])
    student = UserFactory(user_type=UserType.STUDENT)
    qs = Equipment.objects.filter(pk__in=[base.pk, depth.pk])
    assert set(mode_utils.filter_queryset_for_mode_catalog(qs, student, on_date=MONDAY).values_list("pk", flat=True)) == {depth.pk}
    tuesday = MONDAY + timedelta(days=1)
    assert set(mode_utils.filter_queryset_for_mode_catalog(qs, student, on_date=tuesday).values_list("pk", flat=True)) == {
        base.pk, depth.pk
    }


def test_family_conflict_rule_still_applies_during_exclusive(family, egs_factory):
    base, depth, _ = family
    when = egs_factory.future(days=5)
    _schedule(base, depth, when.date(), when.date(), behavior=ModeScheduleBehavior.EXCLUSIVE)
    booking = egs_factory.booking(egs_factory.student(), base, when)
    conflict = mode_utils.family_slots_overlap_conflict(depth, when, when + timedelta(hours=1))
    assert conflict is not None and conflict.booking_id == booking.pk
    later = when + timedelta(days=1)
    assert mode_utils.family_slots_overlap_conflict(depth, later, later + timedelta(hours=1)) is None


def test_staff_bypass_unchanged():
    admin = _admin()
    oic = UserFactory(user_type=UserType.MANAGER)
    student = UserFactory(user_type=UserType.STUDENT)
    assert mode_utils.bypasses_multimode_restrictions(admin)
    assert mode_utils.bypasses_multimode_restrictions(oic)
    assert not mode_utils.bypasses_multimode_restrictions(student)


# --- family link / unlink and invariant -------------------------------------------------------------------------------


def _put_modes(client, base, modes):
    return client.put(f"/api/oic/multi-mode/families/{base.pk}/", {"modes": modes}, format="json")


def test_admin_links_and_unlinks_modes_with_flags_and_audit(egs_factory):
    f = egs_factory
    base = f.equipment(name="APREO")
    ebsd = f.equipment(name="EBSD", enable_multi_mode=True)
    client = f.client_for(_admin())

    res = client.get(f"/api/oic/multi-mode/families/{base.pk}/")
    assert res.status_code == 200
    assert ebsd.pk in {c["equipment_id"] for c in res.data["candidates"]}

    res = _put_modes(client, base, [{"equipment_id": ebsd.pk, "mode_availability": "SCHEDULED_ONLY"}])
    assert res.status_code == 200, res.data
    base.refresh_from_db()
    ebsd.refresh_from_db()
    assert ebsd.parent_equipment_id == base.pk
    assert ebsd.enable_multi_mode is False
    assert ebsd.mode_availability == ModeAvailability.SCHEDULED_ONLY
    assert base.enable_multi_mode is True
    assert EquipmentModeAuditLog.objects.filter(equipment=ebsd, action="MODE_LINKED").exists()
    assert EquipmentModeAuditLog.objects.filter(equipment=base, action="FLAG_SYNC").exists()

    res = _put_modes(client, base, [])
    assert res.status_code == 200, res.data
    base.refresh_from_db()
    ebsd.refresh_from_db()
    assert ebsd.parent_equipment_id is None
    assert base.enable_multi_mode is False
    assert EquipmentModeAuditLog.objects.filter(equipment=ebsd, action="MODE_UNLINKED").exists()


def test_candidates_exclude_other_families_bases_and_other_departments(family, egs_factory):
    f = egs_factory
    base, depth, _ = family
    other_base = f.equipment(name="Other base")
    standalone = f.equipment(name="Standalone")
    other_dept = Department.objects.create(name="Elsewhere", code="ELSW")
    foreign = f.equipment(name="Foreign", internal_department=other_dept)
    client = f.client_for(_admin())

    ids = {c["equipment_id"] for c in client.get(f"/api/oic/multi-mode/families/{other_base.pk}/").data["candidates"]}
    assert standalone.pk in ids
    assert depth.pk not in ids
    assert base.pk not in ids
    assert foreign.pk not in ids

    assert _put_modes(client, other_base, [depth.pk]).status_code == 400
    assert _put_modes(client, other_base, [base.pk]).status_code == 400
    assert _put_modes(client, other_base, [foreign.pk]).status_code == 400
    assert _put_modes(client, depth, [standalone.pk]).status_code == 400


def test_removing_mode_blocked_by_future_booking_or_schedule(family, egs_factory):
    f = egs_factory
    base, depth, ups = family
    client = f.client_for(_admin())
    booking = f.booking(f.student(), depth, f.future(days=4))
    res = _put_modes(client, base, [ups.pk])
    assert res.status_code == 409
    assert booking.virtual_booking_id in res.data["error"]
    assert res.data["blocked_modes"][0]["equipment_id"] == depth.pk
    depth.refresh_from_db()
    assert depth.parent_equipment_id == base.pk

    today = timezone.localdate()
    _schedule(base, ups, today, today + timedelta(days=3))
    res = _put_modes(client, base, [depth.pk])
    assert res.status_code == 409
    assert "schedule" in res.data["error"]

    _schedule(base, ups, today - timedelta(days=30), today - timedelta(days=20))
    EquipmentModeSchedule.objects.filter(mode_equipment=ups, end_date__gte=today).delete()
    res = _put_modes(client, base, [depth.pk])
    assert res.status_code == 200, res.data
    ups.refresh_from_db()
    assert ups.parent_equipment_id is None


def test_mode_cannot_be_flagged_base_model_and_serializer(family):
    from iic_booking.equipment.serializers import EquipmentAdminWriteSerializer

    _, depth, _ = family
    depth.enable_multi_mode = True
    with pytest.raises(ValidationError):
        depth.clean()
    serializer = EquipmentAdminWriteSerializer(instance=depth, data={"enable_multi_mode": True}, partial=True)
    assert not serializer.is_valid()
    assert "enable_multi_mode" in serializer.errors


def test_sync_flags_derives_base_flag_from_modes(egs_factory):
    from iic_booking.equipment.mode_family_service import sync_family_flags

    f = egs_factory
    lonely = f.equipment(name="Flagged without modes", enable_multi_mode=True)
    base = f.equipment(name="Unflagged base")
    mode = f.equipment(name="Flagged mode", parent_equipment=base)
    Equipment.objects.filter(pk=mode.pk).update(enable_multi_mode=True)
    sync_family_flags([lonely.pk, base.pk, mode.pk])
    flags = dict(Equipment.objects.filter(pk__in=[lonely.pk, base.pk, mode.pk]).values_list("pk", "enable_multi_mode"))
    assert flags == {lonely.pk: False, base.pk: True, mode.pk: False}


# --- permissions --------------------------------------------------------------------------------------------------------


def test_oic_sees_and_changes_only_managed_equipment(family, egs_factory):
    f = egs_factory
    base, depth, ups = family
    standalone = f.equipment(name="Mine")
    not_mine = f.equipment(name="Not mine")
    other_base = f.equipment(name="Other base", enable_multi_mode=True)
    f.equipment(name="Other mode", parent_equipment=other_base)
    oic = _oic(f, base, depth, ups, standalone)
    client = f.client_for(oic)

    res = client.get("/api/oic/multi-mode/")
    assert res.status_code == 200
    assert res.data["scope"] == "oic"
    assert [fam["parent_equipment_id"] for fam in res.data["families"]] == [base.pk]
    candidate_ids = {c["equipment_id"] for c in client.get(f"/api/oic/multi-mode/families/{base.pk}/").data["candidates"]}
    assert standalone.pk in candidate_ids and not_mine.pk not in candidate_ids

    assert client.get(f"/api/oic/multi-mode/families/{other_base.pk}/").status_code == 403
    assert _put_modes(client, other_base, []).status_code == 403
    assert _put_modes(client, base, [depth.pk, ups.pk, not_mine.pk]).status_code == 403
    assert _put_modes(client, base, [depth.pk, ups.pk, standalone.pk]).status_code == 200


def test_admin_sees_all_and_filters_by_department(family, egs_factory):
    f = egs_factory
    base, _, _ = family
    other_dept = Department.objects.create(name="Physics", code="PHYS")
    far_base = f.equipment(name="Far base", internal_department=other_dept, enable_multi_mode=True)
    f.equipment(name="Far mode", internal_department=other_dept, parent_equipment=far_base)
    client = f.client_for(_admin())

    ids = {fam["parent_equipment_id"] for fam in client.get("/api/oic/multi-mode/").data["families"]}
    assert {base.pk, far_base.pk} <= ids
    res = client.get("/api/oic/multi-mode/", {"department_id": other_dept.pk})
    assert res.data["scope"] == "admin"
    assert [fam["parent_equipment_id"] for fam in res.data["families"]] == [far_base.pk]
    assert other_dept.pk in {d["id"] for d in res.data["departments"]}


@pytest.mark.parametrize("user_type", [UserType.STUDENT, UserType.OPERATOR, UserType.FACULTY])
def test_other_roles_get_403(family, egs_factory, user_type):
    base, depth, _ = family
    client = egs_factory.client_for(UserFactory(user_type=user_type))
    assert client.get("/api/oic/multi-mode/").status_code == 403
    assert client.get(f"/api/oic/multi-mode/families/{base.pk}/").status_code == 403
    assert _put_modes(client, base, []).status_code == 403
    res = client.post(
        "/api/oic/multi-mode/schedules/",
        {"parent_equipment_id": base.pk, "mode_equipment_id": depth.pk, "start_date": "2030-01-07", "end_date": "2030-01-08"},
        format="json",
    )
    assert res.status_code == 403


def test_department_admin_with_permission_is_scoped_to_department(family, egs_factory):
    f = egs_factory
    base, _, _ = family
    other_dept = Department.objects.create(name="Chem", code="CHEM9")
    far_base = f.equipment(name="Far base", internal_department=other_dept, enable_multi_mode=True)
    f.equipment(name="Far mode", internal_department=other_dept, parent_equipment=far_base)
    dept_admin = UserFactory(user_type=UserType.DEPT_ADMIN, department=f.department)
    client = f.client_for(dept_admin)
    with patch("iic_booking.equipment.mode_family_service.is_department_admin", return_value=True), patch(
        "iic_booking.equipment.mode_family_service.user_has_permission", return_value=True
    ):
        res = client.get("/api/oic/multi-mode/")
        assert res.status_code == 200
        assert [fam["parent_equipment_id"] for fam in res.data["families"]] == [base.pk]
        assert client.get(f"/api/oic/multi-mode/families/{far_base.pk}/").status_code == 403
    with patch("iic_booking.equipment.mode_family_service.is_department_admin", return_value=True), patch(
        "iic_booking.equipment.mode_family_service.user_has_permission", return_value=False
    ):
        assert client.get("/api/oic/multi-mode/").status_code == 403


# --- schedules API -------------------------------------------------------------------------------------------------------


def test_schedule_api_weekdays_and_links_eligible_mode(family, egs_factory):
    f = egs_factory
    base, depth, _ = family
    loose = f.equipment(name="Loose")
    other_base = f.equipment(name="Other base", enable_multi_mode=True)
    taken = f.equipment(name="Taken", parent_equipment=other_base)
    client = f.client_for(_admin())
    payload = {
        "parent_equipment_id": base.pk,
        "mode_equipment_id": depth.pk,
        "start_date": "2030-01-07",
        "end_date": "2030-02-07",
        "weekdays": [3, 0],
        "behavior": "EXCLUSIVE",
    }
    res = client.post("/api/oic/multi-mode/schedules/", payload, format="json")
    assert res.status_code == 201, res.data
    assert res.data["schedule"]["weekdays"] == [0, 3]
    assert res.data["schedule"]["unavailable_label"] == "Mode not scheduled"
    sid = res.data["schedule"]["id"]

    res = client.patch(f"/api/oic/multi-mode/schedules/{sid}/", {"weekdays": []}, format="json")
    assert res.status_code == 200 and res.data["schedule"]["weekdays"] == []
    res = client.patch(f"/api/oic/multi-mode/schedules/{sid}/", {"weekdays": [9]}, format="json")
    assert res.status_code == 400

    res = client.post("/api/oic/multi-mode/schedules/", {**payload, "mode_equipment_id": taken.pk}, format="json")
    assert res.status_code == 400
    taken.refresh_from_db()
    assert taken.parent_equipment_id == other_base.pk

    res = client.post(
        "/api/oic/multi-mode/schedules/", {**payload, "mode_equipment_id": loose.pk, "behavior": "PARALLEL"}, format="json"
    )
    assert res.status_code == 201, res.data
    loose.refresh_from_db()
    assert loose.parent_equipment_id == base.pk

    listing = client.get("/api/oic/multi-mode/").data
    fam = next(x for x in listing["families"] if x["parent_equipment_id"] == base.pk)
    assert {c["equipment_id"]: c["mode_availability"] for c in fam["children"]}[depth.pk] == "ALWAYS"


# --- data migration (production data shape) ------------------------------------------------------------------------------


def test_data_migration_on_production_shaped_families(egs_factory):
    f = egs_factory
    apreo = f.equipment(name="APREO", enable_multi_mode=True)
    ebsd = f.equipment(name="EBSD", enable_multi_mode=True, parent_equipment=apreo)
    nmr = f.equipment(name="NMR", enable_multi_mode=True)
    txi = f.equipment(name="NMR TXI", enable_multi_mode=True, parent_equipment=nmr)
    xps = f.equipment(name="XPS", enable_multi_mode=True)
    depth = f.equipment(name="Depth Profile", enable_multi_mode=True, parent_equipment=xps)
    ups = f.equipment(name="UPS", enable_multi_mode=True, parent_equipment=xps)
    plain = f.equipment(name="Plain")
    _schedule(nmr, txi, MONDAY, MONDAY + timedelta(days=60), behavior=ModeScheduleBehavior.EXCLUSIVE)
    _schedule(xps, depth, MONDAY, MONDAY + timedelta(days=10))
    _schedule(xps, ups, MONDAY + timedelta(days=20), MONDAY + timedelta(days=30))
    others_before = dict(
        Equipment.objects.exclude(pk__in=[apreo.pk, ebsd.pk, nmr.pk, txi.pk, xps.pk, depth.pk, ups.pk, plain.pk])
        .values_list("pk", "enable_multi_mode")
    )
    sched_count = EquipmentModeSchedule.objects.count()

    def bookable(eq, day, *, old_rule=False):
        eq.refresh_from_db()
        if old_rule and eq.parent_equipment_id:
            # Before this change every mode was bookable only inside one of its schedules.
            eq.mode_availability = ModeAvailability.SCHEDULED_ONLY
        return mode_utils.equipment_bookable_on_date(eq, day, time(10))[0]

    before = {
        "depth_in": bookable(depth, MONDAY, old_rule=True),
        "depth_out": bookable(depth, MONDAY + timedelta(days=15), old_rule=True),
        "ups_in": bookable(ups, MONDAY + timedelta(days=25), old_rule=True),
        "ups_out": bookable(ups, MONDAY, old_rule=True),
        "txi_in": bookable(txi, MONDAY, old_rule=True),
        "nmr_in": bookable(nmr, MONDAY, old_rule=True),
        "ebsd": bookable(ebsd, MONDAY, old_rule=True),
    }
    assert before == {
        "depth_in": True, "depth_out": False, "ups_in": True, "ups_out": False,
        "txi_in": True, "nmr_in": False, "ebsd": False,
    }

    migration = importlib.import_module("iic_booking.equipment.migrations.0226_multimode_data_cleanup")
    migration.forward(django_apps, None)

    rows = {e.pk: e for e in Equipment.objects.filter(pk__in=[apreo.pk, ebsd.pk, nmr.pk, txi.pk, xps.pk, depth.pk, ups.pk, plain.pk])}
    for child in (ebsd, txi, depth, ups):
        assert rows[child.pk].enable_multi_mode is False
    for b in (apreo, nmr, xps):
        assert rows[b.pk].enable_multi_mode is True
    assert rows[plain.pk].enable_multi_mode is False
    for child in (ebsd, txi, depth, ups):
        assert rows[child.pk].mode_availability == ModeAvailability.SCHEDULED_ONLY
    assert rows[ebsd.pk].parent_equipment_id == apreo.pk
    assert EquipmentModeSchedule.objects.count() == sched_count
    assert EquipmentModeAuditLog.objects.filter(action="MIGRATION_MODE_CLEANUP").count() == 4
    assert not EquipmentModeAuditLog.objects.filter(action="MIGRATION_BASE_FLAG").exists()

    after = {
        "depth_in": bookable(depth, MONDAY),
        "depth_out": bookable(depth, MONDAY + timedelta(days=15)),
        "ups_in": bookable(ups, MONDAY + timedelta(days=25)),
        "ups_out": bookable(ups, MONDAY),
        "txi_in": bookable(txi, MONDAY),
        "nmr_in": bookable(nmr, MONDAY),
        "ebsd": bookable(ebsd, MONDAY),
    }
    assert after == before

    migration.forward(django_apps, None)
    assert EquipmentModeAuditLog.objects.filter(action="MIGRATION_MODE_CLEANUP").count() == 4

    migration.backward(django_apps, None)
    for child in (ebsd, txi, depth, ups):
        child.refresh_from_db()
        assert child.enable_multi_mode is True
        assert child.mode_availability == ModeAvailability.ALWAYS
        assert child.parent_equipment_id is not None
    assert not EquipmentModeAuditLog.objects.filter(action__startswith="MIGRATION_").exists()
    assert dict(
        Equipment.objects.exclude(pk__in=[apreo.pk, ebsd.pk, nmr.pk, txi.pk, xps.pk, depth.pk, ups.pk, plain.pk])
        .values_list("pk", "enable_multi_mode")
    ) == others_before


def test_data_migration_keeps_modes_of_an_unflagged_base_bookable(egs_factory):
    f = egs_factory
    base = f.equipment(name="Unflagged base")
    mode = f.equipment(name="Loose mode", parent_equipment=base)
    assert mode_utils.equipment_bookable_on_date(mode, MONDAY, time(10))[0] is True

    migration = importlib.import_module("iic_booking.equipment.migrations.0226_multimode_data_cleanup")
    migration.forward(django_apps, None)

    base.refresh_from_db()
    mode.refresh_from_db()
    assert base.enable_multi_mode is True
    assert mode.mode_availability == ModeAvailability.ALWAYS
    assert mode_utils.equipment_bookable_on_date(mode, MONDAY, time(10))[0] is True

    migration.backward(django_apps, None)
    base.refresh_from_db()
    assert base.enable_multi_mode is False
