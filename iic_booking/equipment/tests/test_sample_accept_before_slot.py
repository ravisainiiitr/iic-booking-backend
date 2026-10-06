"""Lab Operator and Officer In Charge record Sample Accepted before the booked slot starts."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from iic_booking.equipment.models import (
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    SampleTraceStatus,
    SlotStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def lab(egs_factory):
    eq = egs_factory.equipment(name="Early XRD")
    student = egs_factory.student()
    booking = egs_factory.booking(student, eq, egs_factory.future(days=5), total_charge="250.00")
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    operator = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    return SimpleNamespace(f=egs_factory, eq=eq, student=student, booking=booking, oic=oic, operator=operator)


def _set_status(lab, user, status, **extra):
    return lab.f.client_for(user).post(
        f"/api/bookings/{lab.booking.pk}/sample-trace/set/", {"status": status, **extra}, format="json"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["operator", "oic"])
def test_lab_staff_accept_sample_days_before_the_slot(lab, who):
    resp = _set_status(lab, getattr(lab, who), SampleTraceStatus.SAMPLE_ACCEPTED)

    assert resp.status_code == 201, resp.content
    assert BookingSampleTrace.objects.filter(
        booking=lab.booking, status=SampleTraceStatus.SAMPLE_ACCEPTED
    ).count() == 1
    lab.booking.refresh_from_db()
    assert lab.booking.status == BookingStatus.BOOKED
    assert lab.booking.total_charge == Decimal("250.00")
    assert set(lab.booking.daily_slots.values_list("status", flat=True)) == {SlotStatus.BOOKED}


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["operator", "oic"])
def test_lab_staff_reject_sample_days_before_the_slot(lab, who, monkeypatch):
    from iic_booking.equipment import api_views

    refunds = []
    monkeypatch.setattr(api_views, "refund_booking_internal", lambda booking, notes, user: refunds.append(booking.pk))

    resp = _set_status(lab, getattr(lab, who), SampleTraceStatus.SAMPLE_REJECTED, reason="Wrong container")

    assert resp.status_code == 201, resp.content
    assert refunds == [lab.booking.pk]


@pytest.mark.django_db
def test_booking_user_still_cannot_accept_own_sample(lab):
    resp = _set_status(lab, lab.student, SampleTraceStatus.SAMPLE_ACCEPTED)

    assert resp.status_code == 403
    assert not BookingSampleTrace.objects.filter(booking=lab.booking).exists()


@pytest.mark.django_db
def test_early_acceptance_does_not_open_booking_not_utilized_before_slot_end(lab):
    assert _set_status(lab, lab.operator, SampleTraceStatus.SAMPLE_ACCEPTED).status_code == 201

    resp = lab.f.client_for(lab.operator).post(f"/api/bookings/{lab.booking.pk}/mark-not-utilized/", {}, format="json")

    assert resp.status_code == 400
    lab.booking.refresh_from_db()
    assert lab.booking.status == BookingStatus.BOOKED
