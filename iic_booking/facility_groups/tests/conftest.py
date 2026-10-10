import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment, EquipmentCategory
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

API = "/api/v1/admin/facility-groups"


def client_for(user=None):
    cl = APIClient()
    if user is not None:
        cl.force_authenticate(user=user)
    return cl


def make_user(**kwargs):
    kwargs.setdefault("email_verified", True)
    kwargs.setdefault("admin_approved", True)
    kwargs.setdefault("email", f"fg{uuid.uuid4().hex[:10]}@iitr.ac.in")
    return UserFactory(**kwargs)


def make_department(name, *, department_type="internal"):
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(name=name, code=f"FG{tag[:4]}", department_type=department_type)


def make_equipment(department=None, **kwargs):
    defaults = {
        "name": f"FG EQ {uuid.uuid4().hex[:4]}",
        "code": f"FG{uuid.uuid4().hex[:6].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
        "internal_department": department,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def make_booking(user, equipment, **kwargs):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    defaults = {
        "user": user,
        "equipment": equipment,
        "charge_profile": profile,
        "user_type_snapshot": user.user_type or "",
        "status": BookingStatus.BOOKED,
        "total_charge": Decimal("10.00"),
        "total_time_minutes": 60,
        "virtual_booking_id": f"FG{equipment.code}{uuid.uuid4().hex[:6]}",
    }
    defaults.update(kwargs)
    return Booking.objects.create(**defaults)


@pytest.fixture(autouse=True)
def _outside_peak_window(monkeypatch):
    monkeypatch.setattr("iic_booking.equipment.peak_window.seconds_until_peak_end_for_deferral", lambda *a, **k: 0)


class World:
    pass


@pytest.fixture
def world(db):
    w = World()
    w.lab = make_department("Rethink ! The Tinkering Lab")
    w.chem = make_department("Chemistry")
    w.org = make_department("Delhi University", department_type="external")
    w.em = EquipmentCategory.objects.create(name="Electron Microscopy", code=f"EM{uuid.uuid4().hex[:4]}")
    w.fesem = make_equipment(w.lab, name="FE-SEM", code=f"FESEM{uuid.uuid4().hex[:4]}", category=w.em)
    w.fesem_mode = make_equipment(None, name="FE-SEM EDS mode", parent_equipment=w.fesem)
    w.tem = make_equipment(w.lab, name="TEM", category=w.em)
    w.xrd = make_equipment(w.chem, name="XRD")
    w.admin = make_user(user_type=UserType.ADMIN, name="Main Admin")
    w.faculty = make_user(user_type=UserType.FACULTY, name="Asha Rao", department=w.chem)
    w.student = make_user(user_type=UserType.STUDENT, name="Student One", department=w.chem, supervisor=w.faculty)
    w.external = make_user(user_type=UserType.EXTERNAL, name="Guest Researcher", department=w.org)
    w.operator = make_user(user_type=UserType.OPERATOR, name="Lab Operator", department=w.lab)
    return w


@pytest.fixture
def run_on_commit(django_capture_on_commit_callbacks):
    """Create bookings with their after-commit membership sync executed."""

    def runner(fn, *args, **kwargs):
        with django_capture_on_commit_callbacks(execute=True):
            return fn(*args, **kwargs)

    return runner
