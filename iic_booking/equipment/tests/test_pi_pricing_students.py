"""Students billed through an Equipment PI's wallet get the "PI IIT Faculty" rates."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import ChargeProfile, ChargeProfilePricingProfile, Equipment, EquipmentPI
from iic_booking.equipment.pi_pricing import get_active_charge_profile, resolve_pricing_profile_for_user
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

PI = ChargeProfilePricingProfile.PI
STANDARD = ChargeProfilePricingProfile.STANDARD


def _student_on_wallet_of(faculty):
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    student = UserFactory(user_type=UserType.STUDENT, admin_approved=True)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    return student


@pytest.fixture
def setup():
    tag = uuid.uuid4().hex[:5].upper()
    dept = Department.objects.create(
        name=f"PI-{tag}", code=f"PI{tag[:4]}", department_type="internal",
        equipment_booking_enabled=True, equipment_visibility_enabled=True,
    )
    eq = Equipment.objects.create(
        name="PI EQ", code=f"PI{tag}", slot_duration_minutes=60, user_rating_enabled=False,
        status="ACTIVE", internal_department=dept, profile_type="SAMPLE",
    )
    rows = {
        "student_std": ChargeProfile.objects.create(
            equipment=eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("100.00")
        ),
        "faculty_std": ChargeProfile.objects.create(
            equipment=eq, user_type=UserType.FACULTY, primary_unit_charge=Decimal("100.00")
        ),
        "faculty_pi": ChargeProfile.objects.create(
            equipment=eq, user_type=UserType.FACULTY, pricing_profile=PI, primary_unit_charge=Decimal("10.00")
        ),
    }
    pi = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    EquipmentPI.objects.create(equipment=eq, faculty=pi)
    other_faculty = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    return eq, rows, pi, other_faculty


def _applied(user, eq):
    pricing = resolve_pricing_profile_for_user(user, eq)
    return get_active_charge_profile(eq, user.user_type, pricing, user)


def test_student_on_pi_wallet_uses_pi_faculty_rates(setup):
    eq, rows, pi, _other = setup
    student = _student_on_wallet_of(pi)
    assert _applied(student, eq) == rows["faculty_pi"]
    assert _applied(pi, eq) == rows["faculty_pi"]


def test_other_students_keep_standard_rates(setup):
    eq, rows, _pi, other_faculty = setup
    assert _applied(_student_on_wallet_of(other_faculty), eq) == rows["student_std"]
    assert _applied(UserFactory(user_type=UserType.STUDENT, admin_approved=True), eq) == rows["student_std"]


def test_student_pi_row_wins_when_defined(setup):
    eq, _rows, pi, _other = setup
    own = ChargeProfile.objects.create(
        equipment=eq, user_type=UserType.STUDENT, pricing_profile=PI, primary_unit_charge=Decimal("5.00")
    )
    assert _applied(_student_on_wallet_of(pi), eq) == own


def test_missing_standard_row_still_raises(setup):
    eq, _rows, _pi, _other = setup
    with pytest.raises(ChargeProfile.DoesNotExist):
        get_active_charge_profile(eq, UserType.EXTERNAL, STANDARD, None)


def test_calculate_endpoint_applies_pi_rates_to_student(setup):
    eq, _rows, pi, _other = setup
    student = _student_on_wallet_of(pi)
    client = APIClient()
    client.force_authenticate(user=student)
    res = client.get(f"/api/equipments/{eq.pk}/calculate/", {"A": "1"})
    assert res.status_code == 200, res.data
    assert res.data["applied_profile"] == "PI"
    assert res.data["pricing"]["wallet_owner_is_pi"] is True
    assert Decimal(res.data["applied_charge"]) < Decimal(res.data["normal_charge"])
