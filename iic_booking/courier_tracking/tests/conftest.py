"""Fixtures for courier tracking. The provider is a scripted fake at the HTTP layer (``requests.get`` inside the
client), so the real client code runs; nothing ever reaches TrackCourier.io."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from iic_booking.courier_tracking.models import Courier, CourierTrackingSettings
from iic_booking.department_modules import services as module_services
from iic_booking.department_modules.constants import ModuleKey
from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


COURIERS = [
    {"slug": "bluedart", "name": "Blue Dart"},
    {"slug": "delhivery", "name": "Delhivery"},
    {"slug": "dtdc", "name": "DTDC"},
    {"slug": "indiapost", "name": "India Post"},
    {"slug": "speedpost", "name": "Speed Post"},
    {"slug": "indiapostinternational", "name": "India Post International"},
    {"slug": "shreemaruti", "name": "Shree Maruti"},
    {"slug": "arccarrier", "name": "ARC Carrier"},
]


def client_for(user=None) -> APIClient:
    cl = APIClient()
    if user is not None:
        cl.force_authenticate(user=user)
    return cl


def make_user(user_type, **kwargs):
    kwargs.setdefault("email_verified", True)
    kwargs.setdefault("admin_approved", True)
    kwargs.setdefault("email", f"ct{uuid.uuid4().hex[:10]}@example.org")
    return UserFactory(user_type=user_type, **kwargs)


def make_department(*, name=None, code=None, iic=False):
    if iic:
        dept = Department.objects.filter(code="IIC").first()
        if dept is None:
            dept = Department.objects.create(name="Institute Instrumentation Centre", code="IIC", department_type="internal")
        return dept
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(name=name or f"CT-Dept-{tag}", code=code or f"CT{tag[:4]}", department_type="internal")


def set_tracking(department, enabled: bool, actor=None):
    actor = actor or make_user(UserType.ADMIN, is_staff=True)
    module_services.set_module(actor, department, ModuleKey.COURIER_TRACKING.value, enabled=enabled,
                               reason="test switch change")


def tracking_on_since(department, when):
    """Record (append-only audit) that Courier Tracking was switched on for the department at ``when``."""
    from iic_booking.department_modules.models import DepartmentModuleAuditLog

    DepartmentModuleAuditLog.objects.create(
        department=department, department_label=department.code, module_key=ModuleKey.COURIER_TRACKING.value,
        action="module.enabled", old_value={"enabled": False}, new_value={"enabled": True}, reason="test",
        created_at=when,
    )


def make_equipment(department):
    tag = uuid.uuid4().hex[:5].upper()
    return Equipment.objects.create(name=f"CT Eq {tag}", code=f"CTE{tag}", internal_department=department)


def make_booking(user, equipment, *, status=BookingStatus.BOOKED, snapshot=None):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=user.user_type, defaults={"primary_unit_charge": Decimal("100")}
    )
    return Booking.objects.create(
        user=user, equipment=equipment, charge_profile=profile, status=status, total_charge=Decimal("100.00"),
        total_time_minutes=60, user_type_snapshot=snapshot or user.user_type,
        virtual_booking_id=f"IICCT{uuid.uuid4().hex[:10]}",
    )


@pytest.fixture(autouse=True)
def courier_templates(request):
    """Transactional tests flush rows the data migrations created; put the courier email templates back.
    (pytest-django's own autouse fixture has already opened the database for marked tests.)"""
    if request.node.get_closest_marker("django_db") is None:
        return
    from importlib import import_module

    from django.apps import apps

    import_module("iic_booking.courier_tracking.migrations.0002_templates_and_schedules").forwards(apps, None)


@pytest.fixture
def no_queue(monkeypatch):
    """Automatic tracking was removed: nothing may queue a courier task any more."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("courier tracking queued a task; automatic tracking was removed")

    for name in ("lookup_task", "send_notifications_task", "process_webhook_task"):
        monkeypatch.setattr(f"iic_booking.courier_tracking.tasks.{name}.delay", _forbidden)


@pytest.fixture
def catalog(db):
    from django.utils import timezone

    for item in COURIERS:
        Courier.objects.update_or_create(slug=item["slug"], defaults={"name": item["name"], "is_active": True})
    cfg = CourierTrackingSettings.load()
    cfg.catalog_refreshed_at = timezone.now()
    cfg.save()
    return COURIERS


@pytest.fixture
def iic(db):
    from datetime import timedelta

    from django.utils import timezone

    dept = make_department(iic=True)
    set_tracking(dept, True)
    tracking_on_since(dept, timezone.now() - timedelta(days=30))  # bookings in the tests are made after go-live
    return dept


@pytest.fixture
def other_dept(db):
    return make_department()


@pytest.fixture
def external_user(db):
    return make_user(UserType.EXTERNAL)


@pytest.fixture
def internal_user(db):
    return make_user(UserType.FACULTY)


@pytest.fixture
def iic_equipment(iic):
    return make_equipment(iic)


@pytest.fixture
def external_booking(external_user, iic_equipment):
    return make_booking(external_user, iic_equipment)
