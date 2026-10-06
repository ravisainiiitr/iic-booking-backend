"""A slot whose booking is completed is shown as Completed (not Booked) in every slot payload."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentGroup,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.serializers import DailySlotSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _equipment(**kwargs):
    return Equipment.objects.create(
        name=f"FE-SEM {uuid.uuid4().hex[:4]}",
        code=f"CS{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        **kwargs,
    )


def _slot(eq, day, hour, *, number):
    master = SlotMaster.objects.create(
        equipment=eq, slot_number=number, open_time=time(hour), close_time=time(hour + 1), is_active=True
    )
    start = timezone.make_aware(datetime.combine(day, time(hour)))
    return DailySlot.objects.create(
        slot_master=master,
        date=day,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=SlotStatus.AVAILABLE,
    )


def _book(owner, eq, slots, *, status=BookingStatus.BOOKED):
    profile = ChargeProfile.objects.create(
        equipment=eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00")
    )
    booking = Booking.objects.create(
        user=owner,
        equipment=eq,
        charge_profile=profile,
        status=status,
        total_charge=Decimal("10.00"),
        total_time_minutes=60 * len(slots),
        virtual_booking_id=f"CS{eq.code}{uuid.uuid4().hex[:6]}",
    )
    DailySlot.objects.filter(pk__in=[s.pk for s in slots]).update(status=SlotStatus.BOOKED, booking=booking)
    return booking


def _get_slots(user, eq, start, end):
    client = APIClient()
    client.force_authenticate(user=user)
    with patch("iic_booking.equipment.api_views.user_can_see_equipment", return_value=True):
        res = client.get(f"/api/equipments/{eq.pk}/slots/", {"start_date": start.isoformat(), "end_date": end.isoformat()})
    assert res.status_code == 200, res.data
    return {row["id"]: row for row in res.data["slots"]}


def _assert_completed(row):
    assert row["status"] == SlotStatus.BOOKED
    assert row["display_status"] == "COMPLETED"
    assert row["status_display"] == "Completed"
    assert row["booking_status"] == BookingStatus.COMPLETED
    assert row["booking_status_display"] == "Completed"


def test_past_slots_of_completed_multi_slot_booking_show_completed_to_users():
    eq = _equipment()
    yesterday = timezone.localdate() - timedelta(days=1)
    slots = [_slot(eq, yesterday, 10, number=1), _slot(eq, yesterday, 11, number=2)]
    owner = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    _book(owner, eq, slots, status=BookingStatus.COMPLETED)
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT)

    rows = _get_slots(student, eq, yesterday, yesterday)

    for s in slots:
        _assert_completed(rows[s.pk])


def test_completed_before_slot_date_is_shown_completed_not_booked_to_everyone():
    eq = _equipment()
    day = timezone.localdate() + timedelta(days=3)
    slot = _slot(eq, day, 10, number=1)
    owner = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    _book(owner, eq, [slot], status=BookingStatus.COMPLETED)
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    admin = UserFactory(admin_approved=True, user_type=UserType.ADMIN)

    _assert_completed(_get_slots(student, eq, day, day)[slot.pk])
    _assert_completed(_get_slots(admin, eq, day, day)[slot.pk])


def test_open_booking_slot_still_shows_booked():
    eq = _equipment()
    day = timezone.localdate() + timedelta(days=2)
    slot = _slot(eq, day, 10, number=1)
    owner = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    _book(owner, eq, [slot], status=BookingStatus.BOOKED)
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT)

    row = _get_slots(student, eq, day, day)[slot.pk]

    assert row["status"] == SlotStatus.BOOKED
    assert row["display_status"] == SlotStatus.BOOKED
    assert row["status_display"] == "Booked"
    assert row["booking_status"] == BookingStatus.BOOKED


def test_external_viewer_sees_completed():
    eq = _equipment()
    day = timezone.localdate() - timedelta(days=1)
    slot = _slot(eq, day, 10, number=1)
    owner = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    _book(owner, eq, [slot], status=BookingStatus.COMPLETED)
    slot = DailySlot.objects.select_related("slot_master", "booking").get(pk=slot.pk)

    data = DailySlotSerializer(slot, context={"for_external_user": True}).data

    _assert_completed(data)


def test_operator_complete_marks_group_equipment_slots_completed_and_keeps_them_occupied():
    group = EquipmentGroup.objects.create(name=f"SEM group {uuid.uuid4().hex[:4]}", code=f"G{uuid.uuid4().hex[:6]}")
    eq = _equipment(equipment_group=group)
    day = timezone.localdate() - timedelta(days=1)
    slots = [_slot(eq, day, 10, number=1), _slot(eq, day, 11, number=2)]
    owner = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    booking = _book(owner, eq, slots, status=BookingStatus.BOOKED)
    operator = UserFactory(admin_approved=True, user_type=UserType.ADMIN)
    client = APIClient()
    client.force_authenticate(user=operator)

    res = client.post(f"/api/bookings/{booking.booking_id}/complete/", {}, format="json")

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.status == BookingStatus.COMPLETED
    assert set(DailySlot.objects.filter(booking=booking).values_list("status", flat=True)) == {SlotStatus.BOOKED}
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    rows = _get_slots(student, eq, day, day)
    for s in slots:
        _assert_completed(rows[s.pk])
