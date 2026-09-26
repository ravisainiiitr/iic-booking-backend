"""Approved repeat sample: user self-books (no auto booking), 48h earliest start, extra week, identity card."""

from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch
import uuid

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.api_views import REPEAT_SAMPLE_BOOKING_DELAY, repeat_sample_extra_week_applies
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    RepeatSampleRequest,
    RepeatSampleRequestStatus,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.pending_actions import collect_pending_actions
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _equipment():
    return Equipment.objects.create(
        name="Repeat EQ",
        code=f"RQ{uuid.uuid4().hex[:4].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        repeat_sample_request_days=7,
    )


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
        input_values={"samples": 3},
        virtual_booking_id=f"IIC{equipment.code}2026{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )


def _slot(equipment, start, slot_number):
    master = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=slot_number,
        open_time=time(9),
        close_time=time(10),
        is_active=True,
    )
    return DailySlot.objects.create(
        slot_master=master,
        date=timezone.localtime(start).date(),
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=SlotStatus.AVAILABLE,
    )


def _user(**kwargs):
    return UserFactory(admin_approved=True, **kwargs)


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def staff_permission():
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        yield


@pytest.fixture
def setup(staff_permission):
    eq = _equipment()
    student = _user(user_type=UserType.STUDENT)
    oic = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    booking = _completed_booking(student, eq)
    req = RepeatSampleRequest.objects.create(
        booking=booking, status=RepeatSampleRequestStatus.PENDING, user_notes="Peaks missing"
    )
    return eq, student, oic, booking, req


def _approve(oic, req):
    return _client_for(oic).post(
        f"/api/repeat-sample-requests/{req.id}/approve/", {"admin_notes": "OK to repeat"}, format="json"
    )


def test_approve_grants_self_booking_without_creating_a_booking(setup):
    eq, student, oic, booking, req = setup
    before = timezone.now()

    res = _approve(oic, req)

    assert res.status_code == 200, res.data
    assert Booking.objects.filter(source_booking=booking).count() == 0
    req.refresh_from_db()
    booking.refresh_from_db()
    assert req.status == RepeatSampleRequestStatus.APPROVED
    assert req.responded_by == oic
    assert req.admin_notes == "OK to repeat"
    assert req.new_booking is None
    assert req.extra_week_granted is True
    assert before + REPEAT_SAMPLE_BOOKING_DELAY <= req.bookable_from <= timezone.now() + REPEAT_SAMPLE_BOOKING_DELAY
    assert booking.repeat_sample_enabled is True
    assert res.data["repeat_sample_request"]["bookable_from"]
    assert res.data["repeat_sample_request"]["responded_by_name"]

    eligibility = _client_for(student).get(f"/api/bookings/{booking.pk}/repeat-sample-eligibility/")
    assert eligibility.status_code == 200, eligibility.data
    assert eligibility.data["can_create_repeat"] is True
    assert eligibility.data["bookable_from"] == req.bookable_from.isoformat()
    assert eligibility.data["extra_week_granted"] is True

    items = {i["key"]: i for i in collect_pending_actions(student)}
    assert items["repeat_sample_ready"]["count"] == 1


def test_repeat_booking_respects_48h_and_is_recorded_against_request(setup):
    eq, student, oic, booking, req = setup
    assert _approve(oic, req).status_code == 200
    req.refresh_from_db()
    too_early = _slot(eq, req.bookable_from - timedelta(hours=2), 1)
    allowed = _slot(eq, req.bookable_from + timedelta(hours=1), 2)

    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked", return_value=(False, "")
    ):
        early = _client_for(student).post(
            f"/api/bookings/{booking.pk}/create-repeat-booking/", {"slot_ids": [too_early.id]}, format="json"
        )
        assert early.status_code == 400, early.data
        assert "48 hours" in early.data["error"]
        assert Booking.objects.filter(source_booking=booking).count() == 0

        ok = _client_for(student).post(
            f"/api/bookings/{booking.pk}/create-repeat-booking/", {"slot_ids": [allowed.id]}, format="json"
        )
    assert ok.status_code == 201, ok.data

    new_booking = Booking.objects.get(source_booking=booking)
    assert new_booking.total_charge == Decimal("0")
    assert new_booking.input_values == {"samples": 3}
    assert new_booking.charge_profile_id == booking.charge_profile_id
    req.refresh_from_db()
    assert req.new_booking_id == new_booking.pk
    assert req.booked_at is not None
    assert req.status == RepeatSampleRequestStatus.APPROVED
    assert "repeat_sample_ready" not in {i["key"] for i in collect_pending_actions(student)}

    record = _client_for(oic).get("/api/repeat-sample-requests/?status=APPROVED").data["repeat_sample_requests"]
    assert len(record) == 1
    assert record[0]["new_real_booking_id"] == new_booking.pk
    assert record[0]["booked_at"]


def test_extra_week_only_for_owner_with_unbooked_approval(setup):
    eq, student, oic, booking, req = setup
    other = _user(user_type=UserType.STUDENT)
    assert repeat_sample_extra_week_applies(student, eq, str(booking.pk)) is False

    assert _approve(oic, req).status_code == 200

    assert repeat_sample_extra_week_applies(student, eq, str(booking.pk)) is True
    assert repeat_sample_extra_week_applies(other, eq, str(booking.pk)) is False
    assert repeat_sample_extra_week_applies(student, _equipment(), str(booking.pk)) is False
    assert repeat_sample_extra_week_applies(student, eq, "not-a-number") is False


def test_identity_card_is_staff_only_and_scoped(setup):
    eq, student, oic, booking, req = setup
    outsider = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=_equipment(), manager=outsider)
    student.phone_number = "9999900000"
    student.degree_name = "Ph.D."
    student.branch_name = "Chemistry"
    student.save(update_fields=["phone_number", "degree_name", "branch_name"])
    url = f"/api/staff/users/{student.pk}/identity-card/"

    assert _client_for(oic).get(url).status_code == 200
    card = _client_for(oic).get(url).data
    assert card["name"] == student.name
    assert card["email"] == student.email
    assert card["phone_number"] == "9999900000"
    assert card["programme"] == "Ph.D. — Chemistry"
    assert "supervisor_name" in card and "department_name" in card and "profile_picture_url" in card

    assert _client_for(outsider).get(url).status_code == 404
    with patch("iic_booking.users.rbac.user_has_permission", return_value=False):
        assert _client_for(student).get(url).status_code == 403
