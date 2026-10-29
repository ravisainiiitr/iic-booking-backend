"""Fixtures for external-user services tests. Everything runs against the disposable test database; no email,
payment or other external call is made (the test settings use the in-memory email backend)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _external_workflow_for_everyone(settings):
    """These tests cover the workflow itself, so it applies to every external user and the internal online-payment
    fallback to every internal user (production pilots both on test accounts; see test_pilot.py)."""
    settings.EXTERNAL_WORKFLOW_ALL_EXTERNAL_USERS = True
    settings.INTERNAL_GATEWAY_FALLBACK_ALL_USERS = True


def client_for(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def make_user(user_type=UserType.EXTERNAL, **kwargs):
    kwargs.setdefault("email", f"ext{uuid.uuid4().hex[:10]}@example.org")
    kwargs.setdefault("admin_approved", True)
    kwargs.setdefault("email_verified", True)
    return UserFactory(user_type=user_type, **kwargs)


def give_wallet(user, department, balance="0.00"):
    """External users book against a department sub-wallet and pay any shortfall by gateway (PENDING_PAYMENT)."""
    from iic_booking.users.models import SubWallet, Wallet

    wallet, _ = Wallet.objects.get_or_create(user=user)
    SubWallet.objects.get_or_create(wallet=wallet, department=department, defaults={"balance": Decimal(balance)})
    return wallet


def main_admin():
    return make_user(UserType.ADMIN, is_staff=True)


def monday(d):
    return d - timedelta(days=d.weekday())


def enable_window(*, opened_days_ago=1, first_week_offset_weeks=1, weeks=4, **extra):
    """Enable the central window relative to the real clock: opened ``opened_days_ago``, first week starts
    ``first_week_offset_weeks`` after this week's Monday."""
    from iic_booking.external_services.models import ExternalBookingWindowConfig

    now = timezone.now()
    config = ExternalBookingWindowConfig.load()
    config.enabled = True
    config.initial_open_at = now - timedelta(days=opened_days_ago)
    config.first_week_start = monday(timezone.localdate()) + timedelta(days=7 * first_week_offset_weeks)
    config.initial_weeks = weeks
    config.auto_expand = False  # keep the range fixed whatever day the tests run
    config.materialized_expansions = 0
    config.counting_from = config.initial_open_at
    config.manual_weeks = 0
    config.min_lead_days = 0
    for k, v in extra.items():
        setattr(config, k, v)
    config.save()
    return config


class Factory:
    """Equipment with an external charge profile and helpers to create slots."""

    def __init__(self):
        from iic_booking.users.models import Department

        tag = uuid.uuid4().hex[:6].upper()
        self.department = Department.objects.create(
            name=f"XS-Dept-{tag}", code=f"XS{tag[:4]}", equipment_booking_enabled=True,
            equipment_visibility_enabled=True,
        )
        self._slot_number = 0

    def equipment(self, **kwargs):
        from iic_booking.equipment.models import ChargeProfile, Equipment, EquipmentProfileType

        defaults = {
            "name": f"XS EQ {uuid.uuid4().hex[:4]}",
            "code": f"XS{uuid.uuid4().hex[:5].upper()}",
            "slot_duration_minutes": 60,
            "user_rating_enabled": False,
            "internal_department": self.department,
            "status": "ACTIVE",
            "external_slot_quota_percent": 100,
        }
        defaults.update(kwargs)
        eq = Equipment.objects.create(**defaults)
        for user_type in (UserType.EXTERNAL, UserType.STUDENT):
            ChargeProfile.objects.create(
                equipment=eq, user_type=user_type, profile_type=EquipmentProfileType.HOUR, time_formula="60",
                primary_unit_charge=Decimal("100.00"),
            )
        return eq

    def slot(self, equipment, start, *, minutes=60, status="AVAILABLE", booking=None):
        from iic_booking.equipment.models import DailySlot, SlotMaster

        self._slot_number += 1
        end = start + timedelta(minutes=minutes)
        master = SlotMaster.objects.create(
            equipment=equipment, slot_number=self._slot_number,
            open_time=timezone.localtime(start).time().replace(microsecond=0),
            close_time=timezone.localtime(end).time().replace(microsecond=0), is_active=True,
        )
        return DailySlot.objects.create(
            slot_master=master, date=timezone.localtime(start).date(), start_datetime=start, end_datetime=end,
            status=status, booking=booking,
        )

    def booking(self, user, equipment, start, *, status="BOOKED", slots=1, total="100.00", **fields):
        """A booking with ``slots`` hourly slots from ``start`` (no booking API involved)."""
        from iic_booking.equipment.models import Booking, ChargeProfile

        profile = ChargeProfile.objects.filter(equipment=equipment, user_type=user.user_type).first() or (
            ChargeProfile.objects.filter(equipment=equipment).first()
        )
        booking = Booking.objects.create(
            user=user, equipment=equipment, charge_profile=profile, status=status, total_charge=Decimal(total),
            total_time_minutes=60 * slots, user_type_snapshot=user.user_type,
            virtual_booking_id=f"IICXS{uuid.uuid4().hex[:8]}", **fields,
        )
        for i in range(slots):
            self.slot(equipment, start + timedelta(minutes=60 * i), status="BOOKED", booking=booking)
        return booking

    @staticmethod
    def at(d, hour=10):
        from datetime import datetime, time

        return timezone.make_aware(datetime.combine(d, time(hour, 0)), timezone.get_current_timezone())


@pytest.fixture
def factory(db):
    return Factory()
