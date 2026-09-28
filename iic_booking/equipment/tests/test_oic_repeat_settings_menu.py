"""OIC-only repeat samples, OIC equipment settings, dashboard menu layout, staff group reschedule, hero count."""

from __future__ import annotations

import importlib
import uuid
from datetime import time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import equipment_group_service as egs
from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentProfileType,
    EquipmentStatus,
    RepeatSampleRequest,
    RepeatSampleRequestStatus,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.sample_submission_deadline_reminders import (
    compute_sample_submission_deadline,
    effective_sample_submission_lead_hours,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _client(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def _user(**kwargs):
    return UserFactory(admin_approved=True, **kwargs)


def _equipment(**kwargs):
    defaults = {
        "name": f"EQ {uuid.uuid4().hex[:4]}",
        "code": f"OR{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _completed_booking(owner, equipment):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        completed_at=timezone.now(),
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        input_values={"samples": 2},
        virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )


def _slot(equipment, start):
    master = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=SlotMaster.objects.filter(equipment=equipment).count() + 1,
        open_time=time(9),
        close_time=time(10),
        is_active=True,
    )
    return DailySlot.objects.create(
        slot_master=master,
        date=timezone.localtime(start).date(),
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=SlotStatus.AVAILABLE,
    )


@pytest.fixture
def repeat_setup():
    eq = _equipment()
    student = _user(user_type=UserType.STUDENT)
    oic = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    booking = _completed_booking(student, eq)
    return eq, student, oic, booking


@pytest.fixture
def no_booking_locks():
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked", return_value=(False, "")
    ):
        yield


# --- Book-for-user picker -------------------------------------------------------------------


def test_oic_book_for_user_list_is_institute_wide():
    from iic_booking.users.models import Department

    tag = uuid.uuid4().hex[:4].upper()
    own = Department.objects.create(name=f"Own {tag}", code=f"OW{tag}")
    other = Department.objects.create(name=f"Other {tag}", code=f"OT{tag}")
    oic = _user(user_type=UserType.MANAGER, department=own)
    same_dept = _user(user_type=UserType.STUDENT, department=own)
    other_dept = _user(user_type=UserType.STUDENT, department=other)
    external = _user(user_type=UserType.RND, department=other)

    res = _client(oic).get("/api/admin/users/", {"lite": "1", "for_booking": "1", "is_active": "1"})
    assert res.status_code == 200, res.data
    rows = res.data if isinstance(res.data, list) else res.data.get("results", [])
    ids = {r["id"] for r in rows}
    assert {same_dept.pk, other_dept.pk, external.pk} <= ids
    assert oic.pk not in ids

    students = _client(oic).get(
        "/api/admin/users/", {"lite": "1", "for_booking": "1", "is_active": "1", "user_type": UserType.STUDENT}
    )
    student_rows = students.data if isinstance(students.data, list) else students.data.get("results", [])
    assert {same_dept.pk, other_dept.pk} <= {r["id"] for r in student_rows}

    info = _client(oic).get(f"/api/admin/users/{other_dept.pk}/booking-info/")
    assert info.status_code == 200, getattr(info, "data", info.content)
    staff = _client(oic).get(f"/api/admin/users/{_user(user_type=UserType.OPERATOR, department=other).pk}/booking-info/")
    assert staff.status_code in (403, 404)


# --- Repeat samples -------------------------------------------------------------------------


def test_oic_marks_repeat_and_books_it_free_for_the_user(repeat_setup, no_booking_locks):
    eq, student, oic, booking = repeat_setup
    slot = _slot(eq, timezone.now() + timedelta(hours=3))

    res = _client(oic).post(
        f"/api/bookings/{booking.pk}/create-repeat-booking/",
        {"slot_ids": [slot.id], "admin_notes": "Peaks missing, genuine"},
        format="json",
    )

    assert res.status_code == 201, res.data
    new_booking = Booking.objects.get(source_booking=booking)
    assert new_booking.user_id == student.pk
    assert new_booking.total_charge == Decimal("0")
    assert new_booking.created_by_id == oic.pk
    slot.refresh_from_db()
    assert slot.booking_id == new_booking.pk
    req = RepeatSampleRequest.objects.get(booking=booking)
    assert req.status == RepeatSampleRequestStatus.APPROVED
    assert req.responded_by_id == oic.pk
    assert req.new_booking_id == new_booking.pk
    assert req.admin_notes == "Peaks missing, genuine"
    event = BookingEvent.objects.get(booking=new_booking, event_type=BookingEventType.REPEAT_SAMPLE_CREATED)
    assert "Officer In Charge" in event.comment

    again = _client(oic).post(f"/api/bookings/{booking.pk}/create-repeat-booking/", {}, format="json")
    assert again.status_code == 400


def test_oic_booking_supersedes_a_pending_user_request(repeat_setup, no_booking_locks):
    eq, student, oic, booking = repeat_setup
    pending = RepeatSampleRequest.objects.create(booking=booking, status=RepeatSampleRequestStatus.PENDING)
    slot = _slot(eq, timezone.now() + timedelta(hours=3))

    res = _client(oic).post(
        f"/api/bookings/{booking.pk}/create-repeat-booking/", {"slot_ids": [slot.id]}, format="json"
    )

    assert res.status_code == 201, res.data
    pending.refresh_from_db()
    assert pending.status == RepeatSampleRequestStatus.APPROVED
    assert pending.new_booking_id is not None


def test_only_oic_of_the_equipment_or_admin_can_book_repeat(repeat_setup, no_booking_locks):
    eq, student, oic, booking = repeat_setup
    other_oic = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=_equipment(), manager=other_oic)
    operator = _user(user_type=UserType.OPERATOR)
    url = f"/api/bookings/{booking.pk}/create-repeat-booking/"

    assert _client(other_oic).post(url, {}, format="json").status_code == 403
    assert _client(operator).post(url, {}, format="json").status_code == 403
    student_res = _client(student).post(url, {}, format="json")
    assert student_res.status_code == 403
    assert student_res.data["code"] == "REPEAT_SAMPLE_BY_OIC_ONLY"
    assert not Booking.objects.filter(source_booking=booking).exists()


def test_users_can_no_longer_request_repeats(repeat_setup):
    eq, student, oic, booking = repeat_setup
    eq.repeat_sample_request_days = 7
    eq.save(update_fields=["repeat_sample_request_days"])

    res = _client(student).post(f"/api/bookings/{booking.pk}/request-repeat-sample/", {}, format="json")
    assert res.status_code == 403
    assert res.data["code"] == "REPEAT_SAMPLE_BY_OIC_ONLY"
    assert not RepeatSampleRequest.objects.exists()

    info = _client(student).get(f"/api/bookings/{booking.pk}/repeat-sample-info/")
    assert info.status_code == 200
    assert info.data["can_request"] is False
    assert info.data["arranged_by_oic"] is True


def test_repeat_request_queue_is_oic_and_admin_only(repeat_setup):
    eq, student, oic, booking = repeat_setup
    operator = _user(user_type=UserType.OPERATOR)
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        assert _client(operator).get("/api/repeat-sample-requests/").status_code == 403
    assert _client(oic).get("/api/repeat-sample-requests/").status_code == 200


def test_migration_closes_pending_user_requests(repeat_setup):
    eq, student, oic, booking = repeat_setup
    pending = RepeatSampleRequest.objects.create(booking=booking, status=RepeatSampleRequestStatus.PENDING)
    other = _completed_booking(student, eq)
    approved = RepeatSampleRequest.objects.create(booking=other, status=RepeatSampleRequestStatus.APPROVED)

    mod = importlib.import_module("iic_booking.equipment.migrations.0201_close_pending_user_repeat_sample_requests")
    mod.close_pending(django_apps, None)

    pending.refresh_from_db()
    approved.refresh_from_db()
    assert pending.status == RepeatSampleRequestStatus.REJECTED
    assert pending.responded_at is not None
    assert "Officer In Charge" in pending.admin_notes
    assert approved.status == RepeatSampleRequestStatus.APPROVED


# --- OIC equipment settings -----------------------------------------------------------------


def test_oic_equipment_settings_list_and_update():
    oic = _user(user_type=UserType.MANAGER)
    mine = _equipment(profile_type=EquipmentProfileType.PRINT_3D)
    EquipmentManager.objects.create(equipment=mine, manager=oic)
    not_mine = _equipment()

    listing = _client(oic).get("/api/oic/equipment-settings/")
    assert listing.status_code == 200, listing.data
    assert [r["equipment_id"] for r in listing.data["equipments"]] == [mine.pk]
    assert listing.data["has_print_3d_equipment"] is True

    res = _client(oic).patch(
        f"/api/oic/equipment-settings/{mine.pk}/",
        {
            "slot_window_reference_weekday": 2,
            "slot_window_reference_time": "21:00",
            "weekly_view_time_from": "09:00",
            "weekly_view_time_to": "18:00",
            "external_slot_quota_percent": 25,
            "booking_not_utilize_window_hours": 12,
            "operator_unavailable_after_booking_end_hours": 6,
            "operator_absent_disruption_after_booking_end_hours": 36,
            "sample_submission_lead_hours": 48,
            "sample_collect_deadline_hours": 96,
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    mine.refresh_from_db()
    assert mine.slot_window_reference_weekday == 2
    assert mine.slot_window_reference_time == time(21, 0)
    assert mine.weekly_view_time_from == time(9, 0) and mine.weekly_view_time_to == time(18, 0)
    assert mine.external_slot_quota_percent == 25
    assert mine.booking_not_utilize_window_hours == 12
    assert mine.operator_unavailable_after_booking_end_hours == 6
    assert mine.operator_absent_disruption_after_booking_end_hours == 36
    assert mine.sample_submission_lead_hours == 48
    assert mine.sample_collect_deadline_hours == 96

    cleared = _client(oic).patch(
        f"/api/oic/equipment-settings/{mine.pk}/",
        {"slot_window_reference_weekday": None, "slot_window_reference_time": ""},
        format="json",
    )
    assert cleared.status_code == 200
    mine.refresh_from_db()
    assert mine.slot_window_reference_weekday is None and mine.slot_window_reference_time is None

    bad = _client(oic).patch(
        f"/api/oic/equipment-settings/{mine.pk}/",
        {"external_slot_quota_percent": 150, "weekly_view_time_from": "19:00"},
        format="json",
    )
    assert bad.status_code == 400
    assert set(bad.data["errors"]) == {"external_slot_quota_percent", "weekly_view_time_to"}

    assert _client(oic).patch(f"/api/oic/equipment-settings/{not_mine.pk}/", {}, format="json").status_code == 403
    student = _user(user_type=UserType.STUDENT)
    assert _client(student).get("/api/oic/equipment-settings/").status_code == 403


def test_oic_without_3d_printer_reports_no_print_equipment():
    oic = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=_equipment(), manager=oic)
    assert _client(oic).get("/api/oic/equipment-settings/").data["has_print_3d_equipment"] is False


# --- Dashboard menu layout ------------------------------------------------------------------


def test_dashboard_menu_layout_saved_per_user():
    oic = _user(user_type=UserType.MANAGER)
    url = "/api/profiles/me/dashboard-menu-layout/"
    assert _client(oic).get(url).data == {"groups": []}

    layout = {
        "groups": [
            {"id": "g1", "name": "  Daily   work ", "items": ["booking_management", "urgent_requests", "booking_management"]},
            {"id": "g2", "name": "Setup", "items": ["accessories"]},
        ]
    }
    res = _client(oic).put(url, layout, format="json")
    assert res.status_code == 200, res.data
    assert res.data["groups"][0] == {"id": "g1", "name": "Daily work", "items": ["booking_management", "urgent_requests"]}
    oic.refresh_from_db()
    assert oic.dashboard_menu_layout["groups"][1]["items"] == ["accessories"]
    assert _client(oic).get(url).data == res.data

    assert _client(oic).put(url, {"groups": [{"id": "g1", "name": "", "items": []}]}, format="json").status_code == 400
    assert _client(oic).put(url, {"groups": "nope"}, format="json").status_code == 400

    student = _user(user_type=UserType.STUDENT)
    assert _client(student).put(url, layout, format="json").status_code == 403
    assert _client(student).get(url).data == {"groups": []}


# --- Staff cross-equipment reschedule -------------------------------------------------------


def test_oic_can_reschedule_onto_group_member_with_flags_off(egs_factory, egs_flags_off, egs_quiet_side_effects):
    group = egs_factory.group()
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group, unit_charge="25.00")
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, source, egs_factory.future(days=4, hour=10))
    target_slot = egs_factory.slot(target, egs_factory.future(days=5, hour=11))
    oic = UserFactory(user_type=UserType.MANAGER)

    owner_options = egs.reschedule_equipment_options(booking, actor=owner)
    assert [o["equipment_id"] for o in owner_options] == [source.pk]
    staff_options = egs.reschedule_equipment_options(booking, actor=oic)
    assert [o["equipment_id"] for o in staff_options] == [source.pk, target.pk]
    assert staff_options[1]["charge_differs"] is True

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = egs_factory.client_for(oic).post(
            f"/api/bookings/{booking.pk}/reschedule/",
            {
                "start_time": target_slot.start_datetime.isoformat(),
                "end_time": target_slot.end_datetime.isoformat(),
                "target_equipment_id": target.pk,
            },
            format="json",
        )
    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.equipment_id == target.pk
    assert booking.total_charge == Decimal("10.00")


def test_staff_group_reschedule_kill_switch(egs_factory, egs_flags_off, settings):
    settings.EQUIPMENT_GROUP_STAFF_CROSS_RESCHEDULING_ENABLED = False
    group = egs_factory.group()
    source = egs_factory.equipment(group)
    egs_factory.equipment(group)
    booking = egs_factory.booking(egs_factory.student(), source, egs_factory.future(days=4, hour=10))
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True)
    assert [o["equipment_id"] for o in egs.reschedule_equipment_options(booking, actor=admin)] == [source.pk]


# --- Hero count and external lead time ------------------------------------------------------


def test_site_stats_counts_all_equipment_for_everyone():
    oic = _user(user_type=UserType.MANAGER)
    managed = _equipment(status=EquipmentStatus.ACTIVE)
    EquipmentManager.objects.create(equipment=managed, manager=oic)
    _equipment(status=EquipmentStatus.REPAIR)
    _equipment(status=EquipmentStatus.DISPOSED)
    expected = Equipment.objects.exclude(status=EquipmentStatus.DISPOSED).count()

    for client in (_client(), _client(oic), _client(_user(user_type=UserType.OPERATOR))):
        res = client.get("/api/cms/site-stats/")
        assert res.status_code == 200
        assert res.data["equipment_count"] == expected


def test_external_samples_have_no_submission_lead_time():
    start = timezone.now() + timedelta(days=3)
    equipment = SimpleNamespace(sample_submission_lead_hours=24)
    internal = SimpleNamespace(atmosphere_sensitive_sample=False, equipment=equipment, user_type_snapshot=UserType.STUDENT)
    external = SimpleNamespace(atmosphere_sensitive_sample=False, equipment=equipment, user_type_snapshot=UserType.RND)

    assert effective_sample_submission_lead_hours(internal) == 24
    assert effective_sample_submission_lead_hours(external) == 0
    with patch(
        "iic_booking.equipment.serializers._booking_slot_bounds", return_value=(start, start + timedelta(hours=1))
    ), patch("iic_booking.equipment.models.Holiday.is_holiday", return_value=(False, None)):
        assert compute_sample_submission_deadline(external) == start
        assert compute_sample_submission_deadline(internal) == start - timedelta(hours=24)
