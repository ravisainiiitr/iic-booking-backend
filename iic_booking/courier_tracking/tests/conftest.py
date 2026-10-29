"""Fixtures for courier tracking. The provider is a scripted fake at the HTTP layer (``requests.get`` inside the
client), so the real client code runs; nothing ever reaches TrackCourier.io."""

from __future__ import annotations

import json
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

FAKE_KEY = "unit-test-trackcourier-key-not-real"
WEBHOOK_SECRET = "unit-test-webhook-signing-secret"

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


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self._text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def track_payload(state="intransit", label="In Transit", checkpoints=None, used=1, quota=100, plan="free"):
    if checkpoints is None:
        checkpoints = [
            {"Activity": "Picked up", "CheckpointState": "inforeceived", "CourierName": "DTDC", "Date": "17-Sep-2026",
             "Time": "10:00", "Location": "Jaipur"},
            {"Activity": "On the way", "CheckpointState": "intransit", "CourierName": "DTDC", "Date": "17-Sep-2026",
             "Time": "22:15", "Location": "Jaipur to Delhi"},
        ]
    return {
        "success": True,
        "data": {"ShipmentState": state, "MostRecentStatus": label, "Checkpoints": checkpoints,
                 "CourierHasNoRecordOfShipment": False, "Result": "success", "PODImageUrl": ""},
        "usage": {"used": used, "quota": quota, "plan": plan, "percent": used},
    }


def error_payload(code, message="error", used=None, quota=None):
    body = {"success": False, "error": {"code": code, "message": message}}
    if used is not None:
        body["usage"] = {"used": used, "quota": quota, "plan": "free", "percent": 100.0}
    return body


class FakeProvider:
    """Scripted responses for ``requests.get``; records each call (path, params, headers)."""

    def __init__(self):
        self.track_responses: list = []
        self.calls: list[dict] = []
        self.couriers_response = FakeResponse(200, {"success": True, "data": {"couriers": COURIERS, "count": len(COURIERS)}})

    def queue(self, *responses):
        self.track_responses.extend(responses)

    @property
    def track_calls(self) -> list[dict]:
        return [c for c in self.calls if c["url"].endswith("/track")]

    def __call__(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout})
        if url.endswith("/couriers"):
            return self.couriers_response
        if url.endswith("/track"):
            if not self.track_responses:
                raise AssertionError("unexpected tracking request")
            item = self.track_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        raise AssertionError(f"unexpected provider path {url}")


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
def provider(settings, monkeypatch):
    settings.TRACKCOURIER_API_KEY = FAKE_KEY
    settings.TRACKCOURIER_API_BASE_URL = "https://api.trackcourier.io/v1"
    fake = FakeProvider()
    monkeypatch.setattr("iic_booking.courier_tracking.client.requests.get", fake)
    return fake


@pytest.fixture
def webhook_secret(settings):
    settings.TRACKCOURIER_WEBHOOK_SECRET = WEBHOOK_SECRET
    return WEBHOOK_SECRET


@pytest.fixture
def no_queue(monkeypatch):
    """Celery is not running in tests: make .delay() fail like an unreachable broker (rows stay for the sweeps)."""
    def _down(*args, **kwargs):
        raise ConnectionError("broker down")

    for name in ("lookup_task", "send_notifications_task", "process_webhook_task"):
        monkeypatch.setattr(f"iic_booking.courier_tracking.tasks.{name}.delay", _down)


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


def signed(body: dict | bytes, secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    import hashlib
    import hmac

    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return raw, "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
