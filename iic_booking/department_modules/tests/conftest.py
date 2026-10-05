import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.department_modules import services
from iic_booking.department_modules.models import DepartmentModulesInstallation
from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

ADMIN_API = "/api/v1/admin/department-modules"


def client_for(user=None):
    cl = APIClient()
    if user is not None:
        cl.force_authenticate(user=user)
    return cl


def make_user(**kwargs):
    kwargs.setdefault("email_verified", True)
    kwargs.setdefault("admin_approved", True)
    kwargs.setdefault("email", f"dm{uuid.uuid4().hex[:10]}@iitr.ac.in")
    return UserFactory(**kwargs)


def make_department(name=None, code=None):
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=name or f"DM-Dept-{tag}", code=code or f"DM{tag[:4]}", department_type="internal"
    )


def make_equipment(department, **kwargs):
    defaults = {
        "name": f"DM EQ {uuid.uuid4().hex[:4]}",
        "code": f"DM{uuid.uuid4().hex[:6].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
        "internal_department": department,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def make_booking(user, equipment, *, created_ago=None, **kwargs):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    defaults = {
        "user": user,
        "equipment": equipment,
        "charge_profile": profile,
        "status": BookingStatus.COMPLETED,
        "total_charge": Decimal("10.00"),
        "total_time_minutes": 60,
        "virtual_booking_id": f"IIC{equipment.code}{uuid.uuid4().hex[:6]}",
    }
    defaults.update(kwargs)
    booking = Booking.objects.create(**defaults)
    if created_ago is not None:
        Booking.objects.filter(pk=booking.pk).update(created_at=timezone.now() - created_ago)
        booking.refresh_from_db()
    return booking


def switch(admin, department, module, *, enabled=None, test_users_only=None, reason="Pilot change for tests"):
    return services.set_module(
        admin, department, module, enabled=enabled, test_users_only=test_users_only, reason=reason
    )


DAY = timedelta(days=1)


@pytest.fixture
def installed(db):
    """Install the switches now, as the seeding migration does in production: later departments start off.

    The root ``conftest.py`` runs the suite with the switches not installed; request this fixture after creating the
    departments that should count as pre-existing.
    """
    return DepartmentModulesInstallation.objects.create(pk=1, installed_at=timezone.now())


class World:
    pass


@pytest.fixture
def world(db):
    w = World()
    w.dept = make_department("Chemistry DM")
    w.other_dept = make_department("Physics DM")
    w.equipment = make_equipment(w.dept, name="FE-SEM DM")
    w.other_equipment = make_equipment(w.other_dept, name="XRD DM")
    w.admin = make_user(user_type=UserType.ADMIN, name="Main Admin")
    w.student = make_user(user_type=UserType.STUDENT, name="Student", department=w.dept)
    w.test_student = make_user(
        user_type=UserType.STUDENT, name="Test Student", department=w.dept, is_test_account=True
    )
    w.faculty = make_user(user_type=UserType.FACULTY, name="Faculty", department=w.dept)
    return w
