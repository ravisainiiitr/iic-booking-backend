"""Booker identity on slot calendars and booking charges for Lab Operators are filtered server-side."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.charge_visibility import BOOKING_MONEY_KEYS
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentOperator,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.serializers import BookingSerializer, DailySlotSerializer
from iic_booking.equipment.slot_booking_identity import SLOT_BOOKING_IDENTITY_FIELDS, SlotBookingIdentityPolicy
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

IDENTITY_KEYS = ("booking", "booking_id", "real_booking_id", "booking_user_name", "booking_user_department_code")


def _equipment(**kwargs):
    return Equipment.objects.create(
        name=f"FV {uuid.uuid4().hex[:4]}",
        code=f"FV{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        **kwargs,
    )


def _slot(eq, day, hour, *, number):
    master = SlotMaster.objects.create(
        equipment=eq, slot_number=number, open_time=time(hour), close_time=time(hour + 1), is_active=True
    )
    start = timezone.make_aware(datetime.combine(day, time(hour)))
    return DailySlot.objects.create(
        slot_master=master,
        date=day,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=SlotStatus.AVAILABLE,
    )


def _book(owner, eq, slots, *, slot_status=SlotStatus.BOOKED):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=eq, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    booking = Booking.objects.create(
        user=owner,
        equipment=eq,
        charge_profile=profile,
        status=BookingStatus.BOOKED,
        total_charge=Decimal("1234.00"),
        total_time_minutes=60 * len(slots),
        virtual_booking_id=f"FV{eq.code}{uuid.uuid4().hex[:6]}",
    )
    DailySlot.objects.filter(pk__in=[s.pk for s in slots]).update(status=slot_status, booking=booking)
    return booking


def _client(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def _slot_rows(user, eq, day):
    """Visibility is patched open so every viewer reaches the payload; only field filtering is under test."""
    params = {"start_date": day.isoformat(), "end_date": day.isoformat()}
    with patch("iic_booking.equipment.api_views.user_can_see_equipment", return_value=True):
        res = _client(user).get(f"/api/equipments/{eq.pk}/slots/", params)
    assert res.status_code == 200, res.data
    return {row["id"]: row for row in res.data["slots"]}


def _assert_masked(row, status=SlotStatus.BOOKED):
    assert row["status"] == status
    for key in IDENTITY_KEYS:
        assert row[key] is None, key
    assert row["booking_user_email"] is None
    assert row["booking_is_external"] is False


def _assert_identity(row, booking):
    assert row["real_booking_id"] == booking.pk
    assert row["booking"] == booking.pk
    assert row["booking_id"]
    assert row["booking_user_name"]


@pytest.fixture
def lab():
    dept = Department.objects.create(name=f"Dept {uuid.uuid4().hex[:5]}", code=f"D{uuid.uuid4().hex[:5]}")
    eq = _equipment(internal_department=dept)
    other_eq = _equipment(internal_department=dept)
    day = timezone.localdate() + timedelta(days=1)
    booked = _slot(eq, day, 10, number=1)
    not_utilized = _slot(eq, day, 12, number=2)
    owner = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    booking = _book(owner, eq, [booked])
    nu_booking = _book(owner, eq, [not_utilized], slot_status=SlotStatus.BOOKING_NOT_UTILIZED)
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    other_oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=other_eq, manager=other_oic)
    operator = UserFactory(admin_approved=True, user_type=UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    other_operator = UserFactory(admin_approved=True, user_type=UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=other_eq, operator=other_operator)
    return SimpleNamespace(
        dept=dept, eq=eq, day=day, booked=booked, not_utilized=not_utilized, owner=owner, booking=booking,
        nu_booking=nu_booking, oic=oic, other_oic=other_oic, operator=operator, other_operator=other_operator,
    )


def test_anonymous_visitor_gets_status_only(lab):
    rows = _slot_rows(None, lab.eq, lab.day)
    _assert_masked(rows[lab.booked.pk])
    _assert_masked(rows[lab.not_utilized.pk], SlotStatus.BOOKING_NOT_UTILIZED)


def test_other_signed_in_user_gets_status_only(lab):
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    rows = _slot_rows(student, lab.eq, lab.day)
    _assert_masked(rows[lab.booked.pk])
    assert rows[lab.booked.pk]["display_status"] == SlotStatus.BOOKED
    _assert_masked(rows[lab.not_utilized.pk], SlotStatus.BOOKING_NOT_UTILIZED)


def test_owner_gets_own_booking_identity(lab):
    rows = _slot_rows(lab.owner, lab.eq, lab.day)
    _assert_identity(rows[lab.booked.pk], lab.booking)
    _assert_identity(rows[lab.not_utilized.pk], lab.nu_booking)
    assert rows[lab.booked.pk]["booking_user_email"] is None


@pytest.mark.parametrize("who", ["oic", "operator", "dept_admin", "admin"])
def test_staff_of_the_equipment_get_booker_identity(lab, who):
    if who == "dept_admin":
        viewer = UserFactory(admin_approved=True, user_type=UserType.DEPT_ADMIN, department=lab.dept)
    elif who == "admin":
        viewer = UserFactory(admin_approved=True, user_type=UserType.ADMIN)
    else:
        viewer = getattr(lab, who)
    rows = _slot_rows(viewer, lab.eq, lab.day)
    _assert_identity(rows[lab.booked.pk], lab.booking)
    _assert_identity(rows[lab.not_utilized.pk], lab.nu_booking)
    assert rows[lab.booked.pk]["booking_user_email"] == lab.owner.email


@pytest.mark.parametrize("who", ["other_oic", "other_operator", "other_dept_admin", "finance"])
def test_staff_of_other_equipment_get_status_only(lab, who):
    if who == "other_dept_admin":
        other_dept = Department.objects.create(name=f"Other {uuid.uuid4().hex[:5]}", code=f"O{uuid.uuid4().hex[:5]}")
        viewer = UserFactory(admin_approved=True, user_type=UserType.DEPT_ADMIN, department=other_dept)
    elif who == "finance":
        viewer = UserFactory(admin_approved=True, user_type=UserType.FINANCE)
    else:
        viewer = getattr(lab, who)
    rows = _slot_rows(viewer, lab.eq, lab.day)
    _assert_masked(rows[lab.booked.pk])
    _assert_masked(rows[lab.not_utilized.pk], SlotStatus.BOOKING_NOT_UTILIZED)


def test_policy_masks_identity_fields_in_serializer(lab):
    slot = DailySlot.objects.select_related("slot_master", "booking__user").get(pk=lab.booked.pk)
    stranger = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    masked = DailySlotSerializer(
        slot, context={"booking_identity_policy": SlotBookingIdentityPolicy(stranger), "include_booking_user_contact": True}
    ).data
    assert all(masked[key] is None for key in SLOT_BOOKING_IDENTITY_FIELDS)
    unmasked = DailySlotSerializer(slot).data
    assert unmasked["booking_user_name"]


def test_admin_daily_slots_list_filters_identity_per_equipment(lab):
    params = {"equipment": lab.eq.pk, "date": lab.day.isoformat()}
    with patch("iic_booking.users.rbac.user_has_admin_panel_access", return_value=True), patch(
        "iic_booking.users.rbac.get_user_department_scope_id", return_value=lab.dept.pk
    ):
        other = _client(lab.other_oic).get("/api/admin/daily-slots/", params)
        mine = _client(lab.oic).get("/api/admin/daily-slots/", params)
    assert other.status_code == 200, other.data
    assert mine.status_code == 200, mine.data
    other_rows = {r["id"]: r for r in other.data["results"]}
    mine_rows = {r["id"]: r for r in mine.data["results"]}
    _assert_masked(other_rows[lab.booked.pk])
    _assert_identity(mine_rows[lab.booked.pk], lab.booking)


def _list(user, **params):
    staff = user.user_type in (UserType.OPERATOR, UserType.MANAGER)
    with patch("iic_booking.equipment.api_views.check_operator_permission", return_value=staff):
        res = _client(user).get("/api/bookings/", params)
    assert res.status_code == 200, res.data
    return res.data["bookings"]


@pytest.mark.parametrize("params", [{"list_view": "1"}, {}])
def test_operator_gets_no_amounts_on_booking_list_and_detail(lab, params):
    rows = _list(lab.operator, equipment_id=lab.eq.pk, **params)
    assert {r["real_booking_id"] for r in rows} >= {lab.booking.pk}
    for row in rows:
        assert not set(BOOKING_MONEY_KEYS) & set(row), row.keys()
    detail = _list(lab.operator, booking_id=lab.booking.pk, limit=1, **params)
    assert len(detail) == 1
    assert "total_charge" not in detail[0]
    assert "amount_paid" not in detail[0]


@pytest.mark.parametrize("params", [{"list_view": "1"}, {}])
def test_oic_and_owner_still_get_amounts(lab, params):
    for viewer in (lab.oic, lab.owner):
        row = next(r for r in _list(viewer, **params) if r["real_booking_id"] == lab.booking.pk)
        assert Decimal(row["total_charge"]) == Decimal("1234.00")
        assert "amount_paid" in row


def test_operator_money_hidden_in_action_responses_serialized_with_viewer(lab):
    data = BookingSerializer(lab.booking, context={"viewer": lab.operator}).data
    assert not set(BOOKING_MONEY_KEYS) & set(data)
    assert "total_charge" in BookingSerializer(lab.booking, context={"viewer": lab.oic}).data


def test_operator_keeps_amounts_on_own_booking(lab):
    own = _book(lab.operator, lab.eq, [_slot(lab.eq, lab.day, 14, number=3)])
    data = BookingSerializer(own, context={"viewer": lab.operator}).data
    assert Decimal(data["total_charge"]) == Decimal("1234.00")
