"""Shared fixtures for Equipment Group alternative / cross-reschedule tests (names prefixed ``egs_``)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework.test import APIClient


class _EgsFactory:
    def __init__(self):
        from iic_booking.users.models import Department

        tag = uuid.uuid4().hex[:6].upper()
        self.department = Department.objects.create(
            name=f"EGS-Dept-{tag}",
            code=f"EG{tag[:4]}",
            equipment_booking_enabled=True,
            equipment_visibility_enabled=True,
        )
        self._slot_number = 0

    def group(self, **switches):
        from iic_booking.equipment.models import EquipmentGroup

        tag = uuid.uuid4().hex[:6].upper()
        return EquipmentGroup.objects.create(name=f"EGS Group {tag}", code=f"EGS-{tag}", **switches)

    def equipment(self, group=None, *, priority=100, with_profile=True, unit_charge="10.00", time_formula="60",
                  **kwargs):
        from iic_booking.equipment.models import ChargeProfile, Equipment, EquipmentProfileType
        from iic_booking.users.models.user_type import UserType

        defaults = {
            "name": f"EGS EQ {uuid.uuid4().hex[:4]}",
            "code": f"EG{uuid.uuid4().hex[:5].upper()}",
            "slot_duration_minutes": 60,
            "user_rating_enabled": False,
            "internal_department": self.department,
            "status": "ACTIVE",
            "equipment_group": group,
            "alternative_priority": priority,
        }
        defaults.update(kwargs)
        eq = Equipment.objects.create(**defaults)
        if with_profile:
            # Formula-based HOUR profile: time = time_formula minutes, charge = hours x unit_charge.
            ChargeProfile.objects.create(
                equipment=eq,
                user_type=UserType.STUDENT,
                profile_type=EquipmentProfileType.HOUR,
                time_formula=time_formula,
                primary_unit_charge=Decimal(unit_charge),
            )
        return eq

    def slot(self, equipment, start, *, minutes=60, status="AVAILABLE", booking=None):
        from iic_booking.equipment.models import DailySlot, SlotMaster

        self._slot_number += 1
        end = start + timedelta(minutes=minutes)
        master = SlotMaster.objects.create(
            equipment=equipment,
            slot_number=self._slot_number,
            open_time=timezone.localtime(start).time().replace(microsecond=0),
            close_time=timezone.localtime(end).time().replace(microsecond=0),
            is_active=True,
        )
        return DailySlot.objects.create(
            slot_master=master,
            date=timezone.localtime(start).date(),
            start_datetime=start,
            end_datetime=end,
            status=status,
            booking=booking,
        )

    def booking(self, owner, equipment, start, *, input_values=None, total_charge="10.00", slot_count=1, **fields):
        from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile
        from iic_booking.users.models.user_type import UserType

        profile = ChargeProfile.objects.filter(equipment=equipment, user_type=UserType.STUDENT).first()
        booking = Booking.objects.create(
            user=owner,
            equipment=equipment,
            charge_profile=profile,
            status=BookingStatus.BOOKED,
            total_charge=Decimal(total_charge),
            total_time_minutes=60 * slot_count,
            input_values=input_values or {},
            virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
            user_type_snapshot=UserType.STUDENT,
            **fields,
        )
        for i in range(slot_count):
            self.slot(equipment, start + timedelta(minutes=60 * i), status="BOOKED", booking=booking)
        return booking

    def student(self):
        from iic_booking.users.models.user_type import UserType
        from iic_booking.users.tests.factories import UserFactory

        return UserFactory(user_type=UserType.STUDENT, department=self.department)

    @staticmethod
    def client_for(user) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    @staticmethod
    def future(days=3, hour=10):
        base = timezone.localtime(timezone.now() + timedelta(days=days))
        return base.replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def egs_factory(db):
    return _EgsFactory()


@pytest.fixture
def egs_flags_on(settings):
    settings.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED = True
    settings.EQUIPMENT_GROUP_CROSS_RESCHEDULING_ENABLED = True
    settings.EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED = False
    return settings


@pytest.fixture
def egs_flags_off(settings):
    settings.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED = False
    settings.EQUIPMENT_GROUP_CROSS_RESCHEDULING_ENABLED = False
    settings.EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED = False
    return settings


@pytest.fixture
def egs_quiet_side_effects(monkeypatch):
    """Keep reschedule tests off email / waitlist / external-quota side effects."""
    from iic_booking.equipment import api_views
    from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService

    calls = SimpleNamespace(waitlist=[], events=[])

    def _notify(equipment, preferred_slot_ids=None, respect_reschedule_threshold=False):
        calls.waitlist.append((equipment.pk, list(preferred_slot_ids or [])))
        return 0

    real_create_event = api_views.create_booking_event

    def _create_event(**kwargs):
        kwargs["send_notification"] = False
        calls.events.append(kwargs)
        return real_create_event(**kwargs)

    monkeypatch.setattr(api_views, "notify_waitlist_slots_available", _notify)
    monkeypatch.setattr(api_views, "create_booking_event", _create_event)
    monkeypatch.setattr(
        ExternalSlotQuotaService,
        "validate_external_booking",
        classmethod(lambda cls, *a, **k: SimpleNamespace(allowed=True)),
    )
    return calls
