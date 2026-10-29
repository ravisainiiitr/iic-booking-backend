"""External users cancel a week's slots free of charge (full wallet refund) until Wednesday 20:30 of the week before."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.external_services.guards import external_free_cancellation, external_reschedule_cutoff
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, enable_window, make_user
from .test_booking_checkout import _balance, _pay, _start, _user, local_gateway  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(factory):
    config = enable_window(weeks=4)
    # The week after next: its Wednesday 20:30 cut-off is still ahead whatever weekday the tests run.
    day = config.first_week_start + timedelta(days=9)
    eq = factory.equipment()
    slots = [factory.slot(eq, factory.at(day, h)) for h in (9, 10)]
    return {"eq": eq, "slots": slots, "dept": factory.department, "factory": factory}


def _booked(user, world):
    body = _start(user, world).json()
    r = _pay(user, body["id"], "full_wallet")
    assert r.status_code == 200, r.content
    return Booking.objects.get(user=user)


def test_external_cancellation_before_the_cutoff_refunds_everything_without_asking(world):
    user = _user(world, "15000.00")
    booking = _booked(user, world)
    assert _balance(user, world) == Decimal("15000.00") - booking.total_charge
    r = client_for(user).post(f"/api/bookings/{booking.booking_id}/user-cancel/", {}, format="json")  # no "refund"
    assert r.status_code == 200, r.content
    assert r.json()["free_cancellation"] is True and "free of charge" in r.json()["message"]
    booking.refresh_from_db()
    assert booking.status in (BookingStatus.CANCELLED, BookingStatus.REFUNDED)
    assert _balance(user, world) == Decimal("15000.00")


def test_cutoff_rule_is_for_external_users_only():
    slot = timezone.now() + timedelta(days=14)
    assert external_free_cancellation(make_user(UserType.EXTERNAL), slot) is True
    assert external_free_cancellation(make_user(UserType.FACULTY), slot) is False


def test_after_the_cutoff_cancellation_is_no_longer_free():
    user = make_user(UserType.EXTERNAL)
    slot = timezone.now() + timedelta(days=14)
    after = external_reschedule_cutoff(slot) + timedelta(minutes=1)
    assert external_free_cancellation(user, slot, now=after) is False
    assert external_free_cancellation(user, slot, now=after - timedelta(minutes=1)) is True
