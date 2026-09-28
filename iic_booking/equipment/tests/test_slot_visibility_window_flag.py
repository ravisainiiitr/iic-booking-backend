"""Staff weekly grid flags slots outside Equipment.weekly_view_time_from/to (hidden from regular users)."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import DailySlot, Equipment, SlotMaster, SlotStatus
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _next_wednesday():
    today = timezone.localdate()
    return today + timedelta(days=((2 - today.weekday()) % 7) or 7)


@pytest.fixture
def windowed_equipment():
    eq = Equipment.objects.create(
        name=f"EQ {uuid.uuid4().hex[:4]}",
        code=f"VW{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        weekly_view_time_from=time(12, 0),
        weekly_view_time_to=time(17, 30),
    )
    day = _next_wednesday()
    slots = {}
    for number, hour in enumerate((9, 13), start=1):
        master = SlotMaster.objects.create(
            equipment=eq, slot_number=number, open_time=time(hour), close_time=time(hour + 1), is_active=True
        )
        start = timezone.make_aware(datetime.combine(day, time(hour)))
        slots[hour] = DailySlot.objects.create(
            slot_master=master,
            date=day,
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            status=SlotStatus.AVAILABLE,
        )
    return eq, day, slots


def _get_slots(user, eq, day):
    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get(f"/api/equipments/{eq.pk}/slots/", {"start_date": day.isoformat(), "end_date": day.isoformat()})
    assert res.status_code == 200, res.data
    return {row["id"]: row for row in res.data["slots"]}


def test_admin_sees_all_slots_with_outside_window_ones_flagged(windowed_equipment):
    eq, day, slots = windowed_equipment
    admin = UserFactory(admin_approved=True, user_type=UserType.ADMIN)

    rows = _get_slots(admin, eq, day)

    assert rows[slots[9].pk]["outside_visibility_window"] is True
    assert rows[slots[13].pk]["outside_visibility_window"] is False


def test_regular_user_only_gets_in_window_slots_without_flag(windowed_equipment):
    eq, day, slots = windowed_equipment
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT)

    with patch("iic_booking.equipment.api_views.user_can_see_equipment", return_value=True):
        rows = _get_slots(student, eq, day)

    assert slots[9].pk not in rows
    assert slots[13].pk in rows
    assert "outside_visibility_window" not in rows[slots[13].pk]


def test_no_flag_when_equipment_has_no_view_window(windowed_equipment):
    eq, day, slots = windowed_equipment
    Equipment.objects.filter(pk=eq.pk).update(weekly_view_time_from=None, weekly_view_time_to=None)
    admin = UserFactory(admin_approved=True, user_type=UserType.ADMIN)

    rows = _get_slots(admin, eq, day)

    assert {slots[9].pk, slots[13].pk} <= set(rows)
    assert all("outside_visibility_window" not in row for row in rows.values())
