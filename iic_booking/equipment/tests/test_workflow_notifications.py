"""In-app notifications and pending-action counts for repeat sample requests."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
import uuid

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.communication.models import CommunicationLog
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    Equipment,
    EquipmentManager,
    RepeatSampleRequest,
    RepeatSampleRequestStatus,
)
from iic_booking.equipment.pending_actions import collect_pending_actions
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _equipment(**kwargs):
    defaults = {
        "name": "Notify EQ",
        "code": f"NQ{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "repeat_sample_request_days": 7,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _completed_booking(owner, equipment):
    profile = ChargeProfile.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        primary_unit_charge=Decimal("10.00"),
    )
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        completed_at=timezone.now(),
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}2026{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )


def _user(**kwargs):
    return UserFactory(admin_approved=True, **kwargs)


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _bell(user):
    return list(
        CommunicationLog.objects.filter(
            recipient=user,
            communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        ).order_by("id")
    )


@pytest.fixture
def staff_permission():
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        yield


def test_repeat_sample_request_notifies_requester_and_oic(django_capture_on_commit_callbacks):
    eq = _equipment()
    student = _user(user_type=UserType.STUDENT)
    oic = _user(user_type=UserType.MANAGER)
    other_oic = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    booking = _completed_booking(student, eq)

    with django_capture_on_commit_callbacks(execute=True):
        res = _client_for(student).post(
            f"/api/bookings/{booking.pk}/request-repeat-sample/", {"user_notes": "Peaks missing"}, format="json"
        )
    assert res.status_code == 201, res.data

    mine = _bell(student)
    assert len(mine) == 1
    assert mine[0].subject == "Repeat sample request submitted"
    assert "Officer in charge" in mine[0].message

    oic_rows = _bell(oic)
    assert len(oic_rows) == 1
    assert oic_rows[0].subject == "Action needed: repeat sample request"
    assert oic_rows[0].metadata["link"] == "/repeat-sample-requests"
    assert oic_rows[0].metadata["action_required"] is True
    assert "Peaks missing" in oic_rows[0].message

    assert _bell(other_oic) == []


def test_repeat_sample_list_and_reject_are_scoped_and_notify(staff_permission, django_capture_on_commit_callbacks):
    eq = _equipment()
    student = _user(user_type=UserType.STUDENT)
    oic = _user(user_type=UserType.MANAGER)
    outsider = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentManager.objects.create(equipment=_equipment(), manager=outsider)
    req = RepeatSampleRequest.objects.create(
        booking=_completed_booking(student, eq), status=RepeatSampleRequestStatus.PENDING
    )

    listed = _client_for(outsider).get("/api/repeat-sample-requests/")
    assert listed.status_code == 200
    assert listed.data["repeat_sample_requests"] == []
    denied = _client_for(outsider).post(f"/api/repeat-sample-requests/{req.id}/reject/", {}, format="json")
    assert denied.status_code == 404

    assert len(_client_for(oic).get("/api/repeat-sample-requests/").data["repeat_sample_requests"]) == 1
    with django_capture_on_commit_callbacks(execute=True):
        ok = _client_for(oic).post(
            f"/api/repeat-sample-requests/{req.id}/reject/", {"admin_notes": "Sample degraded"}, format="json"
        )
    assert ok.status_code == 200, ok.data

    student_rows = _bell(student)
    assert [r.subject for r in student_rows] == ["Repeat sample request rejected"]
    assert "Sample degraded" in student_rows[0].message
    assert [r.subject for r in _bell(oic)] == ["You rejected a repeat sample request"]
    assert _bell(outsider) == []


def test_pending_actions_counts_scoped_repeat_requests(staff_permission):
    eq = _equipment()
    student = _user(user_type=UserType.STUDENT)
    oic = _user(user_type=UserType.MANAGER)
    outsider = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentManager.objects.create(equipment=_equipment(), manager=outsider)
    RepeatSampleRequest.objects.create(
        booking=_completed_booking(student, eq), status=RepeatSampleRequestStatus.PENDING
    )

    items = {i["key"]: i for i in collect_pending_actions(oic)}
    assert items["repeat_sample_requests"]["count"] == 1
    assert items["repeat_sample_requests"]["link"] == "/repeat-sample-requests"

    assert "repeat_sample_requests" not in {i["key"] for i in collect_pending_actions(outsider)}
    assert collect_pending_actions(student) == []

    res = _client_for(oic).get("/api/notifications/pending-actions/")
    assert res.status_code == 200
    assert res.data["total"] >= 1
