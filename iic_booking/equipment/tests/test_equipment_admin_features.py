"""Equipment configuration audit trail, department-based slot reservation switch, operating day across midnight
and OIC request registration."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.test import RequestFactory
from django.utils import timezone

from iic_booking.equipment import booking_audit
from iic_booking.equipment.config_audit import audited_update
from iic_booking.equipment.config_audit_models import ConfigAuditImmutable, EquipmentConfigAuditLog
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    DailySlot,
    Equipment,
    EquipmentManager,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.slot_department_access import (
    equipment_department_slot_policy_active,
    filter_queryset_for_home_department,
    slot_allows_internal_user,
)
from iic_booking.equipment.slot_utils import SlotGenerator
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import SubWalletTransaction
from iic_booking.users.tests.factories import UserFactory

from .test_booking_template_preferred_slot import _book, _student_with_wallet, no_portal_lock  # noqa: F401


def _main_admin():
    return UserFactory(user_type=UserType.ADMIN, admin_approved=True)


def _web_scope(user, path="/api/admin/equipment/1/", origin="https://equip.iitr.ac.in"):
    request = RequestFactory().patch(path, HTTP_ORIGIN=origin, HTTP_X_REQUEST_ID="req-123")
    request.user = user
    return booking_audit.audit_scope(source=booking_audit.SOURCE_WEB, source_label="Web portal", request=request)


def _logs(eq, **filters):
    return list(EquipmentConfigAuditLog.objects.filter(equipment_id=eq.pk, **filters).order_by("id"))


# --- Feature 1: configuration audit trail ---------------------------------------------------------------------


@pytest.mark.django_db
def test_user_change_records_previous_and_new_values_with_actor_and_source(egs_factory):
    eq = egs_factory.equipment(reschedule_hours_threshold=48)
    admin = _main_admin()
    with _web_scope(admin):
        eq.reschedule_hours_threshold = 24
        eq.department_slot_reservation_enabled = True
        eq.save()
    rows = {r.setting: r for r in _logs(eq, action="UPDATED")}
    assert set(rows) == {"reschedule_hours_threshold", "department_slot_reservation_enabled"}
    r = rows["reschedule_hours_threshold"]
    assert (r.previous_value, r.new_value) == ("48", "24")
    assert r.changed_by_id == admin.pk and not r.is_system
    assert r.changed_by_role == UserType.ADMIN
    assert r.source == "DASHBOARD" and r.reference == "req-123"
    assert r.equipment_code == eq.code and r.equipment_name == eq.name
    assert rows["department_slot_reservation_enabled"].new_value == "Yes"


@pytest.mark.django_db
def test_django_admin_and_plain_api_sources(egs_factory):
    eq = egs_factory.equipment(reschedule_hours_threshold=48)
    admin = _main_admin()
    with _web_scope(admin, path="/admin/equipment/equipment/1/change/"):
        eq.reschedule_hours_threshold = 12
        eq.save()
    with _web_scope(admin, origin=""):
        eq.reschedule_hours_threshold = 6
        eq.save()
    assert [r.source for r in _logs(eq, action="UPDATED")] == ["DJANGO_ADMIN", "API"]


@pytest.mark.django_db
def test_system_and_scheduled_task_changes_are_attributed_to_system(egs_factory):
    eq = egs_factory.equipment(reschedule_hours_threshold=48)
    eq.reschedule_hours_threshold = 30
    eq.save()

    class _Task:
        name = "equipment.some_nightly_job"

    booking_audit._on_task_prerun(task_id="t1", task=_Task())
    try:
        eq.reschedule_hours_threshold = 20
        eq.save()
    finally:
        booking_audit._on_task_postrun(task_id="t1")
    plain, task = _logs(eq, action="UPDATED")
    assert plain.is_system and plain.changed_by is None and plain.changed_by_label == "System"
    assert plain.source in ("SYSTEM", "MANAGEMENT_COMMAND")
    assert task.is_system and task.source == "SCHEDULED_TASK"
    assert "equipment.some_nightly_job" in task.process
    assert task.changed_at is not None


@pytest.mark.django_db
def test_unchanged_save_writes_nothing_and_create_delete_are_recorded(egs_factory):
    eq = egs_factory.equipment()
    assert _logs(eq, action="CREATED")
    before = EquipmentConfigAuditLog.objects.count()
    eq.save()
    eq.save(update_fields=["reschedule_hours_threshold"])
    assert EquipmentConfigAuditLog.objects.count() == before

    master = SlotMaster.objects.create(equipment=eq, slot_number=90, open_time=time(9), close_time=time(10))
    master.close_time = time(11)
    master.save()
    row = _logs(eq, object_type="Slot Master", action="UPDATED")[0]
    assert row.setting == "close_time" and row.previous_value == "10:00:00" and row.new_value == "11:00:00"
    master.delete()
    assert _logs(eq, object_type="Slot Master", action="DELETED")


@pytest.mark.django_db
def test_bulk_update_through_audited_update_is_recorded(egs_factory):
    eq = egs_factory.equipment(reschedule_hours_threshold=48)
    audited_update(Equipment.objects.filter(pk=eq.pk), reschedule_hours_threshold=10)
    eq.refresh_from_db()
    assert eq.reschedule_hours_threshold == 10
    (row,) = _logs(eq, action="UPDATED")
    assert (row.previous_value, row.new_value) == ("48", "10")


@pytest.mark.django_db
def test_audit_rows_are_immutable(egs_factory):
    eq = egs_factory.equipment()
    row = _logs(eq)[0]
    row.new_value = "tampered"
    with pytest.raises(ConfigAuditImmutable):
        row.save()
    with pytest.raises(ConfigAuditImmutable):
        row.delete()
    with pytest.raises(ConfigAuditImmutable):
        EquipmentConfigAuditLog.objects.filter(pk=row.pk).update(new_value="x")
    with pytest.raises(ConfigAuditImmutable):
        EquipmentConfigAuditLog.objects.filter(pk=row.pk).delete()


def test_secret_values_are_never_stored():
    from django.db import models

    from iic_booking.equipment.config_audit import REDACTED, _render

    field = models.CharField(name="api_key")
    field.name = "api_key"
    assert _render(field, "abc123") == REDACTED


@pytest.mark.django_db
def test_audit_api_is_main_admin_only_with_filters_and_detail(egs_factory):
    eq = egs_factory.equipment(reschedule_hours_threshold=48)
    other = egs_factory.equipment()
    admin = _main_admin()
    with _web_scope(admin):
        eq.reschedule_hours_threshold = 24
        eq.save()
    client = egs_factory.client_for(admin)
    res = client.get("/api/equipment-config-audit/", {"equipment_id": eq.pk, "setting": "reschedule_hours_threshold"})
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1 and body["results"][0]["new_value"] == "24"
    assert body["results"][0]["changed_by_id"] == admin.pk
    assert client.get("/api/equipment-config-audit/", {"q": other.code}).json()["count"] >= 1
    assert client.get("/api/equipment-config-audit/", {"user_id": admin.pk}).json()["count"] == 1
    today = timezone.localdate().isoformat()
    assert client.get("/api/equipment-config-audit/", {"date_from": today, "date_to": today}).json()["count"] >= 3
    detail = client.get(f"/api/equipment-config-audit/{body['results'][0]['id']}/").json()
    assert detail["reference"] == "req-123" and detail["process"].startswith("PATCH")
    facets = client.get("/api/equipment-config-audit/facets/").json()
    assert any(e["id"] == eq.pk for e in facets["equipment"])

    for user_type in (UserType.MANAGER, UserType.OPERATOR, UserType.DEPT_ADMIN, UserType.STUDENT):
        user = UserFactory(user_type=user_type, department=egs_factory.department, admin_approved=True)
        c = egs_factory.client_for(user)
        assert c.get("/api/equipment-config-audit/").status_code == 403, user_type
        assert c.get("/api/equipment-config-audit/facets/").status_code == 403


# --- Feature 2: department-based slot reservation ------------------------------------------------------------


def _reservation_setup(f, *, enabled):
    eq = f.equipment(department_slot_reservation_enabled=enabled)
    start = f.future(days=4, hour=10)
    unmarked = f.slot(eq, start)
    marked = f.slot(eq, start + timedelta(hours=2))
    DailySlot.objects.filter(pk=marked.pk).update(home_department_only=True)
    marked.refresh_from_db()
    other_dept = Department.objects.create(name=f"Other {eq.code}", code=f"O{eq.code[-4:]}")
    home_user = f.student()
    visitor = UserFactory(user_type=UserType.STUDENT, department=other_dept)
    return eq, unmarked, marked, home_user, visitor


@pytest.mark.django_db
def test_switch_off_keeps_existing_behaviour_even_with_marked_slots(egs_factory):
    eq, unmarked, marked, home_user, visitor = _reservation_setup(egs_factory, enabled=False)
    assert not equipment_department_slot_policy_active(eq)
    for user in (home_user, visitor):
        assert slot_allows_internal_user(unmarked, user, eq)
        assert slot_allows_internal_user(marked, user, eq)


@pytest.mark.django_db
def test_switch_on_splits_slots_between_home_and_other_departments(egs_factory):
    eq, unmarked, marked, home_user, visitor = _reservation_setup(egs_factory, enabled=True)
    assert slot_allows_internal_user(unmarked, home_user, eq)
    assert not slot_allows_internal_user(marked, home_user, eq)
    assert slot_allows_internal_user(marked, visitor, eq)
    assert not slot_allows_internal_user(unmarked, visitor, eq)
    qs = DailySlot.objects.filter(pk__in=[unmarked.pk, marked.pk])
    assert set(filter_queryset_for_home_department(qs, user=visitor, equipment=eq).values_list("pk", flat=True)) == {marked.pk}
    assert set(filter_queryset_for_home_department(qs, user=home_user, equipment=eq).values_list("pk", flat=True)) == {unmarked.pk}


@pytest.mark.django_db
def test_switch_on_without_marks_keeps_every_slot_for_the_home_department(egs_factory):
    eq = egs_factory.equipment(department_slot_reservation_enabled=True)
    slot = egs_factory.slot(eq, egs_factory.future(days=4))
    other = Department.objects.create(name="Visitors", code="VIS9")
    assert slot_allows_internal_user(slot, egs_factory.student(), eq)
    assert not slot_allows_internal_user(slot, UserFactory(user_type=UserType.STUDENT, department=other), eq)


@pytest.mark.django_db
def test_equipment_without_department_is_never_restricted(egs_factory):
    eq = egs_factory.equipment(department_slot_reservation_enabled=True, internal_department=None)
    slot = egs_factory.slot(eq, egs_factory.future(days=4))
    assert not equipment_department_slot_policy_active(eq)
    assert slot_allows_internal_user(slot, UserFactory(user_type=UserType.STUDENT), eq)


@pytest.mark.django_db
def test_direct_booking_api_enforces_the_split(egs_factory, no_portal_lock):  # noqa: F811
    eq, unmarked, marked, home_user, _visitor = _reservation_setup(egs_factory, enabled=True)
    student, _ = _student_with_wallet(egs_factory)
    refused = _book(egs_factory, student, eq, [marked])
    assert refused.status_code in (400, 403), refused.data
    assert _book(egs_factory, student, eq, [unmarked]).status_code == 201


@pytest.mark.django_db
def test_reservation_endpoint_refused_while_switch_is_off(egs_factory):
    eq, unmarked, _marked, _h, _v = _reservation_setup(egs_factory, enabled=False)
    client = egs_factory.client_for(_main_admin())
    url = f"/api/admin/equipment/{eq.pk}/bulk-home-department-only/"
    res = client.post(url, {"home_department_only": True, "slot_ids": [unmarked.pk]}, format="json")
    assert res.status_code == 400 and res.json()["code"] == "DEPARTMENT_SLOT_RESERVATION_OFF"
    Equipment.objects.filter(pk=eq.pk).update(department_slot_reservation_enabled=True)
    res = client.post(url, {"home_department_only": True, "slot_ids": [unmarked.pk]}, format="json")
    assert res.status_code == 200, res.content
    unmarked.refresh_from_db()
    assert unmarked.home_department_only


@pytest.mark.django_db
def test_only_main_admin_may_change_the_new_switches(egs_factory):
    from rest_framework import serializers as drf

    from iic_booking.equipment.serializers import EquipmentAdminWriteSerializer

    eq = egs_factory.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department)
    request = RequestFactory().patch("/")
    for user, allowed in ((oic, False), (_main_admin(), True)):
        request.user = user
        ser = EquipmentAdminWriteSerializer(
            eq, data={"department_slot_reservation_enabled": True}, partial=True, context={"request": request}
        )
        if allowed:
            assert ser.is_valid(), ser.errors
        else:
            with pytest.raises(drf.ValidationError):
                ser.is_valid(raise_exception=True)


@pytest.mark.django_db
def test_migration_keeps_reservations_on_for_equipment_already_using_them(egs_factory):
    import importlib

    mig = importlib.import_module("iic_booking.equipment.migrations.0258_equipment_config_audit_and_slot_cycle")
    eq, _u, _m, _h, _v = _reservation_setup(egs_factory, enabled=False)
    untouched = egs_factory.equipment()
    mig.keep_existing_reservations(django_apps, None)
    eq.refresh_from_db()
    untouched.refresh_from_db()
    assert eq.department_slot_reservation_enabled
    assert not untouched.department_slot_reservation_enabled


# --- Feature 3: operating day across midnight ----------------------------------------------------------------


def _hourly_masters(eq):
    for i in range(24):
        hour = (9 + i) % 24
        SlotMaster.objects.create(
            equipment=eq, slot_number=i + 1, open_time=time(hour), close_time=time((hour + 1) % 24), is_active=True
        )


def _workday(offset=7):
    d = timezone.localdate() + timedelta(days=offset)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


@pytest.mark.django_db
def test_nine_to_nine_cycle_generates_24_consecutive_slots(egs_factory):
    eq = egs_factory.equipment(operating_day_start=time(9), with_profile=False)
    _hourly_masters(eq)
    day = _workday()
    slots = sorted(SlotGenerator.generate_daily_slots(eq, day, allow_holiday=True), key=lambda s: s.start_datetime)
    assert len(slots) == 24
    assert {s.date for s in slots} == {day}
    first, last = timezone.localtime(slots[0].start_datetime), timezone.localtime(slots[-1].end_datetime)
    assert (first.date(), first.time()) == (day, time(9))
    assert (last.date(), last.time()) == (day + timedelta(days=1), time(9))
    tail = timezone.localtime(slots[-1].start_datetime)
    assert (tail.date(), tail.time()) == (day + timedelta(days=1), time(8))
    midnight = [s for s in slots if timezone.localtime(s.start_datetime).time() == time(0)][0]
    assert timezone.localtime(midnight.start_datetime).date() == day + timedelta(days=1)
    for a, b in zip(slots, slots[1:]):
        assert a.end_datetime == b.start_datetime


@pytest.mark.django_db
def test_consecutive_cycles_neither_overlap_nor_duplicate(egs_factory):
    eq = egs_factory.equipment(operating_day_start=time(9), with_profile=False)
    _hourly_masters(eq)
    day = _workday()
    SlotGenerator.generate_slots_for_week(eq, day, day + timedelta(days=1), allow_holiday=True)
    SlotGenerator.generate_slots_for_week(eq, day, day + timedelta(days=1), allow_holiday=True)
    rows = list(DailySlot.objects.filter(slot_master__equipment=eq).order_by("start_datetime"))
    assert len(rows) == 48
    starts = [r.start_datetime for r in rows]
    assert len(set(starts)) == 48
    for a, b in zip(rows, rows[1:]):
        assert a.end_datetime <= b.start_datetime


@pytest.mark.django_db
def test_calendar_day_equipment_is_unchanged(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    _hourly_masters(eq)
    day = _workday()
    slots = SlotGenerator.generate_daily_slots(eq, day, allow_holiday=True)
    assert all(timezone.localtime(s.start_datetime).date() == day for s in slots)


@pytest.mark.django_db
def test_changing_the_operating_day_retimes_free_slots_and_refuses_while_booked_slots_would_move(egs_factory):
    from django.core.exceptions import ValidationError

    eq = egs_factory.equipment(with_profile=False)
    _hourly_masters(eq)
    day = _workday()
    SlotGenerator.generate_daily_slots(eq, day, allow_holiday=True)
    early = DailySlot.objects.get(slot_master__equipment=eq, date=day, slot_master__open_time=time(2))

    eq.operating_day_start = time(9)
    eq.full_clean(exclude=["image", "video_file"])
    eq.save()
    early.refresh_from_db()
    assert timezone.localtime(early.start_datetime).date() == day + timedelta(days=1)

    early.status = SlotStatus.BOOKED
    early.save(update_fields=["status"])
    eq.operating_day_start = None
    with pytest.raises(ValidationError) as exc:
        eq.full_clean(exclude=["image", "video_file"])
    assert "operating_day_start" in exc.value.message_dict


# --- Feature 4: OIC request registration -----------------------------------------------------------------------


def _oic_for(f, eq):
    """An OIC (Manager); managers have no wallet of their own."""
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic, None


def _register(f, user, eq, slot):
    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
        "waitlist_on_failure": False,
        "oic_registration": True,
        "notes": "Repair of a lab power supply",
    }
    return f.client_for(user).post(f"/api/equipments/{eq.pk}/book/", body, format="json")


@pytest.mark.django_db
def test_oic_registers_a_request_in_own_name_without_charge(egs_factory, no_portal_lock):  # noqa: F811
    eq = egs_factory.equipment(oic_request_registration_enabled=True)
    oic, _ = _oic_for(egs_factory, eq)
    slot = egs_factory.slot(eq, egs_factory.future(days=3))
    debits_before = SubWalletTransaction.objects.filter(transaction_type="debit").count()
    res = _register(egs_factory, oic, eq, slot)
    assert res.status_code == 201, res.data
    booking = eq.bookings.get()
    assert booking.user_id == oic.pk
    assert booking.total_charge == Decimal("0.00")
    assert booking.status == "BOOKED"
    assert SubWalletTransaction.objects.filter(transaction_type="debit").count() == debits_before
    slot.refresh_from_db()
    assert slot.booking_id == booking.pk and slot.status == "BOOKED"
    created = BookingEvent.objects.get(booking=booking, event_type=BookingEventType.CREATED)
    assert created.metadata.get("oic_registered") is True

    listing = egs_factory.client_for(oic).get(f"/api/equipments/{eq.pk}/oic-requests/")
    assert listing.status_code == 200
    assert [r["booking_id"] for r in listing.json()["results"]] == [booking.booking_id]


@pytest.mark.django_db
def test_registration_is_off_by_default(egs_factory, no_portal_lock):  # noqa: F811
    eq = egs_factory.equipment()
    assert eq.oic_request_registration_enabled is False
    oic, _ = _oic_for(egs_factory, eq)
    res = _register(egs_factory, oic, eq, egs_factory.slot(eq, egs_factory.future(days=3)))
    assert res.status_code == 403 and res.data["code"] == "OIC_REGISTRATION_NOT_ALLOWED"
    assert egs_factory.client_for(oic).get(f"/api/equipments/{eq.pk}/oic-requests/").status_code == 403


@pytest.mark.django_db
def test_other_oics_and_users_cannot_register(egs_factory, no_portal_lock):  # noqa: F811
    eq = egs_factory.equipment(oic_request_registration_enabled=True)
    elsewhere = egs_factory.equipment()
    other_oic, _ = _oic_for(egs_factory, elsewhere)
    slot = egs_factory.slot(eq, egs_factory.future(days=3))
    assert _register(egs_factory, other_oic, eq, slot).status_code == 403
    student, _ = _student_with_wallet(egs_factory)
    assert _register(egs_factory, student, eq, slot).status_code == 403
    assert not eq.bookings.exists()


@pytest.mark.django_db
def test_migration_switches_oic_registration_on_for_the_iic_repair_lab_only(egs_factory):
    import importlib

    mig = importlib.import_module("iic_booking.equipment.migrations.0258_equipment_config_audit_and_slot_cycle")
    iic, _ = Department.objects.get_or_create(code="IIC", defaults={"name": "Institute Instrumentation Centre"})
    lab = egs_factory.equipment(code="ERL IIC", internal_department=iic)
    same_code_elsewhere = egs_factory.equipment(code="ERL IIC2")
    other = egs_factory.equipment()
    mig.enable_oic_registration_for_repair_lab(django_apps, None)
    for eq in (lab, same_code_elsewhere, other):
        eq.refresh_from_db()
    assert lab.oic_request_registration_enabled
    assert not same_code_elsewhere.oic_request_registration_enabled and not other.oic_request_registration_enabled
