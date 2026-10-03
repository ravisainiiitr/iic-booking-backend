"""The urgent request expiry applies to the whole portal: staff who manage bookings can view it,
only the Main Administrator can change it (OICs, temporary OICs, Lab Operators and Department
Administrators get 403)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    ChargeProfile,
    Equipment,
    EquipmentProfileType,
    EquipmentTemporaryOIC,
    UrgentHoldExpiryConfig,
)
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "/api/urgent-booking-requests/hold-expiry-config/"


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _user(**kwargs):
    return UserFactory(admin_approved=True, **kwargs)


@pytest.fixture
def department():
    tag = uuid.uuid4().hex[:4].upper()
    return Department.objects.create(name=f"Chem {tag}", code=f"CH{tag}", department_type=DepartmentType.INTERNAL)


@pytest.fixture
def config():
    return UrgentHoldExpiryConfig.objects.create(hold_expiry_hours=24, urgent_booking_validity_days=3)


def _validity_days() -> int:
    return UrgentHoldExpiryConfig.objects.first().urgent_booking_validity_days


def _assert_read_only(user):
    client = _client(user)
    res = client.get(URL)
    assert res.status_code == 200
    assert res.data["urgent_booking_validity_days"] == 3
    res = client.patch(URL, {"urgent_booking_validity_days": 9}, format="json")
    assert res.status_code == 403
    assert res.data["code"] == "URGENT_EXPIRY_GLOBAL"
    assert _validity_days() == 3


def test_oic_can_view_but_not_change(config, department):
    _assert_read_only(_user(user_type=UserType.MANAGER, department=department))


def test_temporary_oic_can_view_but_not_change(config, department):
    primary = _user(user_type=UserType.MANAGER, department=department)
    temporary = _user(user_type=UserType.MANAGER, department=department)
    eq = Equipment.objects.create(
        name=f"EQ {uuid.uuid4().hex[:4]}",
        code=f"TX{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        profile_type=EquipmentProfileType.HOUR,
        internal_department=department,
    )
    ChargeProfile.objects.create(equipment=eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("0"))
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=primary, temporary_oic=temporary, resume_at=timezone.now() + timedelta(days=3)
    )
    _assert_read_only(temporary)


def test_lab_operator_cannot_change(config, department):
    res = _client(_user(user_type=UserType.OPERATOR, department=department)).patch(
        URL, {"urgent_booking_validity_days": 9}, format="json"
    )
    assert res.status_code == 403
    assert _validity_days() == 3


def test_main_admin_can_change(config):
    res = _client(_user(user_type=UserType.ADMIN, is_staff=True)).patch(
        URL, {"urgent_booking_validity_days": 9}, format="json"
    )
    assert res.status_code == 200
    assert res.data["urgent_booking_validity_days"] == 9
    assert _validity_days() == 9


def test_end_user_cannot_view_or_change(config):
    client = _client(_user(user_type=UserType.STUDENT))
    assert client.get(URL).status_code == 403
    assert client.patch(URL, {"urgent_booking_validity_days": 9}, format="json").status_code == 403
    assert _validity_days() == 3
