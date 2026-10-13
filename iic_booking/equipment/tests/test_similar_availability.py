"""'Next available on similar instruments' and the low-rating alert to the instrument's staff."""

from __future__ import annotations

import uuid
from datetime import time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.communication.models import CommunicationLog
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentCategory,
    EquipmentGroup,
    EquipmentManager,
    EquipmentStatus,
    SlotMaster,
    SlotStatus,
)
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _department():
    tag = uuid.uuid4().hex[:4].upper()
    return Department.objects.create(
        name=f"Similar {tag}", code=f"SM{tag}", department_type=DepartmentType.INTERNAL,
        equipment_visibility_enabled=True,
    )


def _equipment(name, category, department, **extra):
    return Equipment.objects.create(
        name=name,
        code=f"SA{uuid.uuid4().hex[:4].upper()}",
        slot_duration_minutes=60,
        category=category,
        internal_department=department,
        status=extra.pop("status", EquipmentStatus.ACTIVE),
        **extra,
    )


def _slot(equipment, days_ahead, hour, status=SlotStatus.AVAILABLE, number=None):
    master, _ = SlotMaster.objects.get_or_create(
        equipment=equipment, slot_number=number or hour, defaults={"open_time": time(hour), "close_time": time(hour + 1)}
    )
    day = timezone.localdate() + timedelta(days=days_ahead)
    start = timezone.make_aware(timezone.datetime.combine(day, time(hour)))
    return DailySlot.objects.create(
        slot_master=master, date=day, start_datetime=start, end_datetime=start + timedelta(hours=1), status=status
    )


@pytest.fixture
def world():
    dept = _department()
    xrd = EquipmentCategory.objects.create(name="X-ray diffraction", code=f"XRD{uuid.uuid4().hex[:4]}")
    sem = EquipmentCategory.objects.create(name="Microscopy", code=f"SEM{uuid.uuid4().hex[:4]}")
    full = _equipment("PXRD A", xrd, dept)
    mode = _equipment("PXRD A thin-film mode", xrd, dept, parent_equipment=full)
    sibling_late = _equipment("PXRD B", xrd, dept)
    sibling_soon = _equipment("PXRD C", xrd, dept)
    broken = _equipment("PXRD D", xrd, dept, status=EquipmentStatus.REPAIR)
    other = _equipment("FESEM", sem, dept)
    _slot(full, 20, 10)
    _slot(full, 1, 10, status=SlotStatus.BOOKED)
    _slot(mode, 1, 11)
    _slot(sibling_late, 6, 9)
    _slot(sibling_soon, 2, 14)
    _slot(broken, 1, 9)
    _slot(other, 1, 9)
    user = UserFactory(admin_approved=True, user_type=UserType.STUDENT, department=dept)
    return {"full": full, "late": sibling_late, "soon": sibling_soon, "user": user}


def test_similar_instruments_sorted_by_earliest_open_slot(world):
    client = APIClient()
    client.force_authenticate(user=world["user"])
    resp = client.get(f"/api/equipments/{world['full'].pk}/similar-availability/")
    assert resp.status_code == 200
    body = resp.json()
    # The only open slot on this instrument is 20 days out, past every user's booking window.
    assert body["equipment"]["earliest"] is None
    names = [row["name"] for row in body["alternatives"]]
    assert names == ["PXRD C", "PXRD B"] and body["basis"] == "category"
    assert body["alternatives"][0]["earliest"]["date"] == (timezone.localdate() + timedelta(days=2)).isoformat()


def test_equipment_group_takes_precedence_over_category(world):
    group = EquipmentGroup.objects.create(name=f"XRD pool {uuid.uuid4().hex[:4]}")
    Equipment.objects.filter(pk__in=[world["full"].pk, world["late"].pk]).update(equipment_group=group)
    client = APIClient()
    client.force_authenticate(user=world["user"])
    body = client.get(f"/api/equipments/{world['full'].pk}/similar-availability/").json()
    assert body["basis"] == "group"
    assert [row["name"] for row in body["alternatives"]] == ["PXRD B"]


def test_similar_availability_needs_sign_in_and_visible_equipment(world):
    assert APIClient().get(f"/api/equipments/{world['full'].pk}/similar-availability/").status_code in (401, 403)
    client = APIClient()
    client.force_authenticate(user=world["user"])
    assert client.get("/api/equipments/999999/similar-availability/").status_code == 404


def _completed_booking(user, equipment):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    return Booking.objects.create(
        user=user, equipment=equipment, charge_profile=profile, status=BookingStatus.COMPLETED,
        total_charge=Decimal("10.00"), total_time_minutes=60, virtual_booking_id="IICLOW202600001",
    )


def _rate(user, booking, yes: bool, feedback="Operator was late"):
    client = APIClient()
    client.force_authenticate(user=user)
    keys = [
        "on_time_operator_availability", "laboratory_cleanliness_organization", "sample_handling_care",
        "operator_behaviour_professionalism", "compliance_booking_request_parameters",
    ]
    return client.post(
        f"/api/bookings/{booking.booking_id}/rate/", {**{k: yes for k in keys}, "feedback": feedback}, format="json"
    )


def test_low_rating_alerts_the_oic_once(world, django_capture_on_commit_callbacks):
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=world["full"], manager=oic)
    booking = _completed_booking(world["user"], world["full"])

    with django_capture_on_commit_callbacks(execute=True):
        assert _rate(world["user"], booking, yes=False).status_code == 200
    alerts = CommunicationLog.objects.filter(recipient=oic, subject__startswith="Low rating")
    assert alerts.count() == 1
    assert "Operator was late" in alerts.first().message
    assert alerts.first().metadata["notification_category"] == "staff"

    with django_capture_on_commit_callbacks(execute=True):
        _rate(world["user"], booking, yes=False, feedback="Still unhappy")
    assert alerts.count() == 1

    with django_capture_on_commit_callbacks(execute=True):
        _rate(world["user"], booking, yes=True)
    assert alerts.count() == 1
