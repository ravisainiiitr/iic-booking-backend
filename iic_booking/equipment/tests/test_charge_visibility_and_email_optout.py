"""Internal IITR rates are hidden from anonymous/external viewers; per-staff booking-confirmation email opt-out."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth.models import AnonymousUser
from rest_framework.test import APIClient

from iic_booking.equipment.charge_visibility import viewer_may_see_internal_rates
from iic_booking.equipment.models import ChargeProfile, Equipment, EquipmentManager, EquipmentOperator
from iic_booking.equipment.reports import get_booking_confirmation_email_opt_out_user_ids
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _dept(kind="internal"):
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"CV-{tag}", code=f"CV{tag[:4]}", department_type=kind,
        equipment_booking_enabled=True, equipment_visibility_enabled=True,
    )


def _equipment():
    eq = Equipment.objects.create(
        name="Visibility EQ",
        code=f"CV{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        status="ACTIVE",
        internal_department=_dept(),
    )
    for ut in (UserType.STUDENT, UserType.FACULTY, UserType.EXTERNAL):
        ChargeProfile.objects.create(equipment=eq, user_type=ut, primary_unit_charge=Decimal("100.00"))
    return eq


def _client(user=None) -> APIClient:
    c = APIClient()
    if user is not None:
        c.force_authenticate(user=user)
    return c


def test_viewer_may_see_internal_rates_matrix():
    assert viewer_may_see_internal_rates(None) is False
    assert viewer_may_see_internal_rates(AnonymousUser()) is False
    assert viewer_may_see_internal_rates(UserFactory(user_type=UserType.EXTERNAL)) is False
    assert viewer_may_see_internal_rates(UserFactory(user_type=UserType.STUDENT, department=_dept())) is True
    assert viewer_may_see_internal_rates(UserFactory(user_type=UserType.FACULTY, department=_dept())) is True
    assert viewer_may_see_internal_rates(UserFactory(user_type=UserType.FACULTY, department=_dept("external"))) is False
    assert viewer_may_see_internal_rates(UserFactory(user_type=UserType.MANAGER)) is True


def _profile_types(payload):
    return {str(p.get("user_type")).lower() for p in payload.get("charge_profiles") or []}


def test_equipment_detail_hides_internal_profiles_from_anonymous_and_external():
    eq = _equipment()
    internal = {UserType.STUDENT, UserType.FACULTY}

    anon = _client().get(f"/api/equipments/{eq.pk}/")
    assert anon.status_code == 200, anon.content
    assert not (_profile_types(anon.json()) & internal)

    ext = _client(UserFactory(user_type=UserType.EXTERNAL, admin_approved=True)).get(f"/api/equipments/{eq.pk}/")
    assert not (_profile_types(ext.json()) & internal)
    assert UserType.EXTERNAL in _profile_types(ext.json())

    stu = _client(UserFactory(user_type=UserType.STUDENT, department=_dept(), admin_approved=True)).get(
        f"/api/equipments/{eq.pk}/"
    )
    assert internal <= _profile_types(stu.json())


def test_calculate_refuses_internal_estimates_for_anonymous():
    eq = _equipment()
    res = _client().get(f"/api/equipments/{eq.pk}/calculate/?user_type=student")
    assert res.status_code == 403
    res = _client().get(f"/api/equipments/{eq.pk}/calculate/")
    assert res.status_code == 400
    assert "user category" in res.json()["error"]


def test_calculate_for_staff_without_target_user_gives_clear_message():
    eq = _equipment()
    manager = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=manager)
    res = _client(manager).get(f"/api/equipments/{eq.pk}/calculate/")
    assert res.status_code == 400
    assert "Select a user" in res.json()["error"]


def test_booking_confirmation_opt_out_requires_every_role_opted_out():
    eq = _equipment()
    oic_out = UserFactory(user_type=UserType.MANAGER)
    oic_in = UserFactory(user_type=UserType.MANAGER)
    both = UserFactory(user_type=UserType.OPERATOR)
    lic_out = UserFactory(user_type=UserType.OPERATOR)
    EquipmentManager.objects.create(equipment=eq, manager=oic_out, disable_booking_confirmation_email=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic_in)
    EquipmentManager.objects.create(equipment=eq, manager=both, disable_booking_confirmation_email=True)
    EquipmentOperator.objects.create(equipment=eq, operator=both)
    EquipmentOperator.objects.create(
        equipment=eq,
        operator=lic_out,
        role=EquipmentOperator.Role.SECONDARY,
        disable_booking_confirmation_email=True,
    )

    assert get_booking_confirmation_email_opt_out_user_ids(eq) == {oic_out.id, lic_out.id}
    assert get_booking_confirmation_email_opt_out_user_ids(None) == set()
