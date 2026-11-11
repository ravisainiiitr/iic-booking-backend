import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.connect.models import Community
from iic_booking.connect.models import CommunityKind
from iic_booking.connect.models import CommunityMembership
from iic_booking.connect.models import DiscussionPost
from iic_booking.connect.models import DiscussionThread
from iic_booking.equipment.models import Booking
from iic_booking.equipment.models import BookingStatus
from iic_booking.equipment.models import ChargeProfile
from iic_booking.equipment.models import Equipment
from iic_booking.equipment.models import EquipmentGroup
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

API = "/api/v1/connect"


@pytest.fixture(autouse=True)
def _connect_seed(request):
    """Re-run the create-only seed of migration 0002 (General community, categories, badges).

    Transactional tests flush the database, and ``--reuse-db`` keeps that flushed database for the next run, so
    tests must not rely on migration data surviving.
    """
    marker = request.node.get_closest_marker("django_db")
    if marker is None:
        return
    transactional = bool(marker.kwargs.get("transaction"))
    request.getfixturevalue("transactional_db" if transactional else "db")
    import importlib

    from django.apps import apps as django_apps

    importlib.import_module("iic_booking.connect.migrations.0002_seed_general_categories").seed(django_apps, None)


@pytest.fixture(autouse=True)
def _clear_connect_settings_cache():
    from iic_booking.connect.services.threads import clear_settings_cache

    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def connect_on(settings):
    settings.IITR_CONNECT_ENABLED = True
    settings.IITR_CONNECT_PILOT_EMAILS = ""
    return settings


class _FreshAccessClient(APIClient):
    """force_authenticate reuses one user object across requests; production loads a fresh user per request, so
    clear the per-request access snapshot before each call to match."""

    def request(self, **kwargs):
        from iic_booking.connect import access

        user = getattr(self, "_connect_user", None)
        if user is not None:
            access.invalidate(user)
        return super().request(**kwargs)


def client_for(user=None):
    cl = _FreshAccessClient()
    if user is not None:
        cl.force_authenticate(user=user)
        cl._connect_user = user
    return cl


def make_department(name="Dept", *, department_type="internal"):
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(name=f"{name} {tag}", code=f"CN{tag[:4]}", department_type=department_type)


def make_user(user_type=UserType.STUDENT, **kwargs):
    kwargs.setdefault("email_verified", True)
    kwargs.setdefault("admin_approved", True)
    kwargs.setdefault("email", f"cn{uuid.uuid4().hex[:10]}@iitr.ac.in")
    return UserFactory(user_type=user_type, **kwargs)


def make_external(**kwargs):
    kwargs.setdefault("email", f"ext{uuid.uuid4().hex[:8]}@industry.example")
    return make_user(UserType.INSTITUTE, **kwargs)


def make_group(name=None):
    return EquipmentGroup.objects.create(name=name or f"Group {uuid.uuid4().hex[:6]}")


_VISIBLE_DEPARTMENT = {}


def visible_department():
    """An internal department whose catalogue is visible (equipment of hidden departments is not listed)."""
    dep = _VISIBLE_DEPARTMENT.get("dep")
    if dep is None or not Department.objects.filter(pk=dep.pk).exists():
        dep = make_department("Visible Dept")
        Department.objects.filter(pk=dep.pk).update(equipment_visibility_enabled=True)
        dep.refresh_from_db()
        _VISIBLE_DEPARTMENT["dep"] = dep
    return dep


def make_equipment(group=None, department=None, **kwargs):
    defaults = {
        "name": f"CN EQ {uuid.uuid4().hex[:4]}",
        "code": f"CN{uuid.uuid4().hex[:6].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
        "internal_department": department or visible_department(),
        "equipment_group": group,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def make_booking(user, equipment, status=BookingStatus.COMPLETED, **kwargs):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    defaults = {
        "user": user,
        "equipment": equipment,
        "charge_profile": profile,
        "user_type_snapshot": user.user_type or "",
        "status": status,
        "total_charge": Decimal("10.00"),
        "total_time_minutes": 60,
        "virtual_booking_id": f"CN{equipment.code}{uuid.uuid4().hex[:6]}",
    }
    defaults.update(kwargs)
    return Booking.objects.create(**defaults)


@pytest.fixture(autouse=True)
def _outside_peak_window(monkeypatch):
    monkeypatch.setattr("iic_booking.equipment.peak_window.seconds_until_peak_end_for_deferral", lambda *a, **k: 0)


def general_community():
    return Community.objects.get(kind=CommunityKind.GENERAL)


def group_community(group=None, **kwargs):
    group = group or make_group()
    return Community.objects.create(
        kind=CommunityKind.GROUP,
        slug=f"g-{group.pk}-{uuid.uuid4().hex[:4]}",
        name=group.name,
        equipment_group=group,
        equipment_group_ref_id=group.pk,
        group_name_snapshot=group.name,
        **kwargs,
    )


def add_member(community, user, **kwargs):
    kwargs.setdefault("via_booking", True)
    return CommunityMembership.objects.create(community=community, user=user, **kwargs)


def make_thread(community, author, title="How to calibrate the detector?", body="<p>Details</p>", **kwargs):
    thread = DiscussionThread.objects.create(
        community=community, author=author, title=title, last_activity_at=timezone.now(), **kwargs
    )
    DiscussionPost.objects.create(thread=thread, author=author, is_opening=True, body_html=body, body_text=body)
    return thread
