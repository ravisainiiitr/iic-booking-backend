"""Booking Not Utilized honours the equipment's "Booking Not Utilize Window (hours)" everywhere."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.equipment import booking_not_utilized_service as svc
from iic_booking.equipment.models import (
    BookingSampleTrace,
    BookingStatus,
    EquipmentOperator,
    SampleTraceStatus,
    SlotStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _no_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(svc, "send_booking_not_utilized_emails", lambda booking, slots, **kw: sent.append(booking.pk))
    return sent


def _booking_ended(f, window, hours_ago):
    eq = f.equipment(
        booking_not_utilize_window_hours=window, sample_submission_lead_hours=24, sample_collect_deadline_hours=72
    )
    start = timezone.now() - timedelta(hours=hours_ago, minutes=60)
    return f.booking(f.student(), eq, start)


def _status(booking):
    booking.refresh_from_db()
    return booking.status


@pytest.mark.django_db
def test_automatic_marking_waits_for_the_equipment_window(egs_factory):
    early = _booking_ended(egs_factory, 240, hours_ago=26)
    due = _booking_ended(egs_factory, 240, hours_ago=241)

    assert not svc.apply_booking_not_utilized(early, actor=None, automated=True, hours_after_last_slot_end=24)
    assert _status(early) == BookingStatus.BOOKED
    assert set(early.daily_slots.values_list("status", flat=True)) == {SlotStatus.BOOKED}
    assert not BookingSampleTrace.objects.filter(booking=early).exists()

    assert svc.apply_booking_not_utilized(due, actor=None, automated=True, hours_after_last_slot_end=24)
    assert _status(due) == BookingStatus.BOOKING_NOT_UTILIZED
    assert set(due.daily_slots.values_list("status", flat=True)) == {SlotStatus.BOOKING_NOT_UTILIZED}
    trace = BookingSampleTrace.objects.get(booking=due)
    assert trace.status == SampleTraceStatus.NOT_UTILIZED
    assert "240 hours" in trace.reason


@pytest.mark.django_db
def test_window_zero_switches_booking_not_utilized_off(egs_factory):
    booking = _booking_ended(egs_factory, 0, hours_ago=500)

    assert not svc.apply_booking_not_utilized(booking, actor=None, automated=True, hours_after_last_slot_end=24)
    assert not svc.apply_booking_not_utilized(booking, actor=None, automated=False, hours_after_last_slot_end=0)
    assert _status(booking) == BookingStatus.BOOKED


def test_auto_hours_never_below_24():
    from types import SimpleNamespace

    assert svc.auto_not_utilized_hours(SimpleNamespace(booking_not_utilize_window_hours=240)) == 240
    assert svc.auto_not_utilized_hours(SimpleNamespace(booking_not_utilize_window_hours=6)) == 24
    assert svc.auto_not_utilized_hours(SimpleNamespace(booking_not_utilize_window_hours=0)) is None


@pytest.mark.django_db
def test_scheduled_check_uses_each_equipments_window(egs_factory):
    from iic_booking.equipment.tasks import check_booking_not_utilized

    long_window = _booking_ended(egs_factory, 240, hours_ago=30)
    day_window = _booking_ended(egs_factory, 24, hours_ago=30)
    short_window = _booking_ended(egs_factory, 6, hours_ago=12)
    off = _booking_ended(egs_factory, 0, hours_ago=300)

    with patch("iic_booking.equipment.models.Holiday.is_holiday", return_value=(False, None)):
        marked = check_booking_not_utilized()

    assert marked == 1
    assert _status(day_window) == BookingStatus.BOOKING_NOT_UTILIZED
    assert _status(long_window) == BookingStatus.BOOKED
    assert _status(short_window) == BookingStatus.BOOKED
    assert _status(off) == BookingStatus.BOOKED


def _operator_for(f, booking):
    operator = UserFactory(user_type=UserType.OPERATOR, department=f.department, admin_approved=True)
    EquipmentOperator.objects.create(equipment=booking.equipment, operator=operator)
    return operator


@pytest.mark.django_db
def test_staff_cannot_mark_before_the_window(egs_factory):
    booking = _booking_ended(egs_factory, 240, hours_ago=30)
    client = egs_factory.client_for(_operator_for(egs_factory, booking))

    resp = client.post(f"/api/bookings/{booking.pk}/mark-not-utilized/", {}, format="json")

    assert resp.status_code == 400
    assert "240 hours" in resp.data["error"]
    assert _status(booking) == BookingStatus.BOOKED


@pytest.mark.django_db
def test_staff_can_mark_after_the_window(egs_factory):
    booking = _booking_ended(egs_factory, 12, hours_ago=13)
    client = egs_factory.client_for(_operator_for(egs_factory, booking))

    resp = client.post(f"/api/bookings/{booking.pk}/mark-not-utilized/", {}, format="json")

    assert resp.status_code == 200, resp.data
    assert _status(booking) == BookingStatus.BOOKING_NOT_UTILIZED


@pytest.mark.django_db
def test_staff_action_off_when_window_is_zero(egs_factory):
    booking = _booking_ended(egs_factory, 0, hours_ago=100)
    client = egs_factory.client_for(_operator_for(egs_factory, booking))

    resp = client.post(f"/api/bookings/{booking.pk}/mark-not-utilized/", {}, format="json")

    assert resp.status_code == 400
    assert _status(booking) == BookingStatus.BOOKED


@pytest.mark.django_db
def test_reconcile_command_honours_the_window(egs_factory):
    from io import StringIO

    from django.core.management import call_command

    early = _booking_ended(egs_factory, 240, hours_ago=30)
    due = _booking_ended(egs_factory, 24, hours_ago=30)

    call_command("reconcile_stale_booked_bookings", stdout=StringIO())

    assert _status(early) == BookingStatus.BOOKED
    assert _status(due) == BookingStatus.BOOKING_NOT_UTILIZED
