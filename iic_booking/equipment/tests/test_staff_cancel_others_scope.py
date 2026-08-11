"""Unit + API tests for staff cancel of other users' bookings (admin / dept_admin / OIC)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import uuid

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.booking_cancellation import actor_may_cancel_booking
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    SlotMaster,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def test_actor_may_cancel_booking_admin_any():
    admin = SimpleNamespace(
        is_authenticated=True,
        is_superuser=False,
        user_type=UserType.ADMIN,
        id=1,
        department_id=None,
    )
    booking = SimpleNamespace(equipment=SimpleNamespace(internal_department_id=99), equipment_id=5)
    assert actor_may_cancel_booking(admin, booking) is None


def test_actor_may_cancel_booking_dept_admin_scope():
    dept_admin = SimpleNamespace(
        is_authenticated=True,
        is_superuser=False,
        user_type=UserType.DEPT_ADMIN,
        id=2,
        department_id=10,
    )
    same = SimpleNamespace(
        equipment=SimpleNamespace(internal_department_id=10),
        equipment_id=1,
    )
    other = SimpleNamespace(
        equipment=SimpleNamespace(internal_department_id=20),
        equipment_id=2,
    )
    assert actor_may_cancel_booking(dept_admin, same) is None
    err = actor_may_cancel_booking(dept_admin, other)
    assert err and "department" in err.lower()


@patch("iic_booking.equipment.reports.get_equipment_ids_managed_by_oic", return_value=[101])
def test_actor_may_cancel_booking_oic_scope(mock_ids):
    oic = SimpleNamespace(
        is_authenticated=True,
        is_superuser=False,
        user_type=UserType.MANAGER,
        id=3,
        department_id=None,
    )
    assigned = SimpleNamespace(equipment=None, equipment_id=101)
    unassigned = SimpleNamespace(equipment=None, equipment_id=202)
    assert actor_may_cancel_booking(oic, assigned) is None
    err = actor_may_cancel_booking(oic, unassigned)
    assert err and ("assigned" in err.lower() or "officer" in err.lower())
    mock_ids.assert_called()


def test_actor_may_cancel_booking_non_privileged_denied():
    student = SimpleNamespace(
        is_authenticated=True,
        is_superuser=False,
        user_type=UserType.STUDENT,
        id=4,
        department_id=None,
    )
    operator = SimpleNamespace(
        is_authenticated=True,
        is_superuser=False,
        user_type=UserType.OPERATOR,
        id=5,
        department_id=None,
    )
    booking = SimpleNamespace(equipment=SimpleNamespace(internal_department_id=1), equipment_id=1)
    assert actor_may_cancel_booking(student, booking) is not None
    assert actor_may_cancel_booking(operator, booking) is not None
    assert actor_may_cancel_booking(None, booking) is not None


def _department(*, suffix: str | None = None):
    tag = suffix or uuid.uuid4().hex[:6].upper()
    return Department.objects.create(name=f"CancelDept-{tag}", code=f"CD{tag[:4]}")


def _equipment(*, department=None, **kwargs):
    defaults = {
        "name": "Cancel Scope EQ",
        "code": f"CX{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "internal_department": department,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _booking(owner, equipment, *, status=BookingStatus.BOOKED):
    profile = ChargeProfile.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        primary_unit_charge=Decimal("10.00"),
    )
    booking = Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=status,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}2026{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )
    now = timezone.now()
    start = now + timedelta(days=3)
    end = start + timedelta(hours=1)
    slot_master = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=1,
        open_time=start.time().replace(microsecond=0),
        close_time=end.time().replace(microsecond=0),
        is_active=True,
    )
    DailySlot.objects.create(
        slot_master=slot_master,
        date=start.date(),
        start_datetime=start,
        end_datetime=end,
        status="BOOKED",
        booking=booking,
    )
    return booking


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
@patch("iic_booking.equipment.api_views.perform_booking_cancellation")
def test_admin_can_cancel_any_booking(mock_cancel):
    mock_cancel.return_value = {
        "released_slot_ids": [1],
        "refund_transaction": None,
        "refund_amount": Decimal("0"),
        "is_full_cancel": True,
    }
    dept = _department()
    eq = _equipment(department=dept)
    owner = UserFactory(user_type=UserType.STUDENT)
    booking = _booking(owner, eq)
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True)

    res = _client_for(admin).post(f"/api/bookings/{booking.pk}/cancel/", {"refund": False}, format="json")
    assert res.status_code == 200, res.data
    mock_cancel.assert_called_once()


@pytest.mark.django_db
@patch("iic_booking.equipment.api_views.perform_booking_cancellation")
def test_dept_admin_same_department_ok_other_denied(mock_cancel):
    mock_cancel.return_value = {
        "released_slot_ids": [1],
        "refund_transaction": None,
        "refund_amount": Decimal("0"),
        "is_full_cancel": True,
    }
    dept_a = _department(suffix="DA")
    dept_b = _department(suffix="DB")
    eq_a = _equipment(department=dept_a)
    eq_b = _equipment(department=dept_b)
    owner = UserFactory(user_type=UserType.STUDENT)
    booking_a = _booking(owner, eq_a)
    booking_b = _booking(owner, eq_b)
    dept_admin = UserFactory(user_type=UserType.DEPT_ADMIN, department=dept_a)
    client = _client_for(dept_admin)

    ok = client.post(f"/api/bookings/{booking_a.pk}/cancel/", {"refund": False}, format="json")
    assert ok.status_code == 200, ok.data

    denied = client.post(f"/api/bookings/{booking_b.pk}/cancel/", {"refund": False}, format="json")
    assert denied.status_code == 403
    assert "department" in str(denied.data.get("error", "")).lower()
    assert mock_cancel.call_count == 1


@pytest.mark.django_db
@patch("iic_booking.equipment.api_views.perform_booking_cancellation")
def test_oic_assigned_equipment_ok_unassigned_denied(mock_cancel):
    mock_cancel.return_value = {
        "released_slot_ids": [1],
        "refund_transaction": None,
        "refund_amount": Decimal("0"),
        "is_full_cancel": True,
    }
    dept = _department()
    eq_assigned = _equipment(department=dept)
    eq_other = _equipment(department=dept)
    owner = UserFactory(user_type=UserType.STUDENT)
    booking_ok = _booking(owner, eq_assigned)
    booking_denied = _booking(owner, eq_other)
    oic = UserFactory(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq_assigned, manager=oic)
    client = _client_for(oic)

    ok = client.post(f"/api/bookings/{booking_ok.pk}/cancel/", {"refund": False}, format="json")
    assert ok.status_code == 200, ok.data

    denied = client.post(
        f"/api/bookings/{booking_denied.pk}/cancel/", {"refund": False}, format="json"
    )
    assert denied.status_code == 403
    assert "officer" in str(denied.data.get("error", "")).lower() or "assigned" in str(
        denied.data.get("error", "")
    ).lower()
    assert mock_cancel.call_count == 1


@pytest.mark.django_db
@patch("iic_booking.equipment.api_views.perform_booking_cancellation")
def test_non_privileged_user_cannot_cancel_others(mock_cancel):
    dept = _department()
    eq = _equipment(department=dept)
    owner = UserFactory(user_type=UserType.STUDENT)
    other = UserFactory(user_type=UserType.STUDENT)
    booking = _booking(owner, eq)

    res = _client_for(other).post(f"/api/bookings/{booking.pk}/cancel/", {"refund": False}, format="json")
    assert res.status_code == 403
    mock_cancel.assert_not_called()

    user_cancel = _client_for(other).post(
        f"/api/bookings/{booking.pk}/user-cancel/", {"refund": True}, format="json"
    )
    assert user_cancel.status_code == 403
    mock_cancel.assert_not_called()
