"""OIC manually confirms a waitlisted request into any unbooked slot (maintenance, closed, holiday)."""

from __future__ import annotations

import uuid
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentOperator,
    EquipmentProfileType,
    SlotMaster,
    SlotStatus,
    WaitlistEntry,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _slot(equipment, start, status):
    master = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=SlotMaster.objects.filter(equipment=equipment).count() + 1,
        open_time=time(9),
        close_time=time(10),
        is_active=True,
    )
    return DailySlot.objects.create(
        slot_master=master,
        date=timezone.localtime(start).date(),
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=status,
    )


@pytest.fixture
def setup():
    eq = Equipment.objects.create(
        name=f"EQ {uuid.uuid4().hex[:4]}",
        code=f"WL{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        profile_type=EquipmentProfileType.HOUR,
    )
    ChargeProfile.objects.create(equipment=eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("0"))
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    entry = WaitlistEntry.objects.create(equipment=eq, user=student)
    return eq, student, oic, entry


@pytest.fixture
def wallet_ok():
    target = MagicMock()
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked", return_value=(False, "")
    ), patch(
        "iic_booking.equipment.waitlist_booking.WalletRepository.get_booking_wallet_target",
        return_value=(target, None),
    ), patch(
        "iic_booking.users.wallet_credit_facility.subwallet_booking_balance_ok", return_value=(True, "")
    ):
        yield


def test_oic_confirms_waitlist_entry_into_maintenance_slot(setup, wallet_ok):
    eq, student, oic, entry = setup
    slot = _slot(eq, timezone.now() + timedelta(days=2), SlotStatus.UNDER_MAINTENANCE)

    listing = _client(oic).get(
        f"/api/admin/equipment/{eq.pk}/waitlist-slots/",
        {"date": slot.date.isoformat(), "entry_id": entry.id},
    )
    assert listing.status_code == 200, listing.data
    row = next(r for r in listing.data["slots"] if r["id"] == slot.id)
    assert row["status"] == SlotStatus.UNDER_MAINTENANCE
    assert row["selectable"] is True

    res = _client(oic).post(
        f"/api/admin/equipment/{eq.pk}/waitlist-confirm/",
        {"entry_id": entry.id, "slot_ids": [slot.id]},
        format="json",
    )
    assert res.status_code == 201, res.data
    booking = Booking.objects.get(booking_id=res.data["booking_id"])
    assert booking.user_id == student.pk
    assert booking.created_by_id == oic.pk
    assert "Confirmed manually from waitlist" in booking.notes
    slot.refresh_from_db()
    assert slot.booking_id == booking.pk
    assert slot.status == SlotStatus.BOOKED
    assert not WaitlistEntry.objects.filter(pk=entry.pk).exists()


def test_manual_confirm_rejects_booked_slot_and_lab_operator(setup, wallet_ok):
    eq, _student, oic, entry = setup
    slot = _slot(eq, timezone.now() + timedelta(days=3), SlotStatus.BOOKED)

    res = _client(oic).post(
        f"/api/admin/equipment/{eq.pk}/waitlist-confirm/",
        {"entry_id": entry.id, "slot_ids": [slot.id]},
        format="json",
    )
    assert res.status_code == 400
    assert WaitlistEntry.objects.filter(pk=entry.pk).exists()

    operator = UserFactory(admin_approved=True, user_type=UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    free = _slot(eq, timezone.now() + timedelta(days=4), SlotStatus.NOT_AVAILABLE)
    res = _client(operator).post(
        f"/api/admin/equipment/{eq.pk}/waitlist-confirm/",
        {"entry_id": entry.id, "slot_ids": [free.id]},
        format="json",
    )
    assert res.status_code == 403
