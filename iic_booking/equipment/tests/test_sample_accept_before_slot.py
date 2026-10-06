"""Sample Accepted / Rejected by lab staff: before the booked slot, and only on their own equipment."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.equipment.models import (
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    SampleTraceStatus,
    SlotStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def lab(egs_factory):
    dept = egs_factory.department
    eq = egs_factory.equipment(name="Early XRD")
    other_eq = egs_factory.equipment(name="Other SEM")
    student = egs_factory.student()
    booking = egs_factory.booking(student, eq, egs_factory.future(days=5), total_charge="250.00")
    oic = UserFactory(user_type=UserType.MANAGER, department=dept, admin_approved=True)
    operator = UserFactory(user_type=UserType.OPERATOR, department=dept, admin_approved=True)
    other_oic = UserFactory(user_type=UserType.MANAGER, department=dept, admin_approved=True)
    other_operator = UserFactory(user_type=UserType.OPERATOR, department=dept, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    EquipmentManager.objects.create(equipment=other_eq, manager=other_oic)
    EquipmentOperator.objects.create(equipment=other_eq, operator=other_operator)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    return SimpleNamespace(
        f=egs_factory, eq=eq, student=student, booking=booking, oic=oic, operator=operator,
        other_oic=other_oic, other_operator=other_operator, admin=admin,
    )


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
@pytest.mark.parametrize("status", [SampleTraceStatus.SAMPLE_ACCEPTED, SampleTraceStatus.SAMPLE_REJECTED])
@pytest.mark.parametrize(
    "who, message",
    [
        ("other_oic", "equipment you are Officer In-charge of"),
        ("other_operator", "equipment you are assigned to as Lab Operator"),
    ],
)
def test_staff_of_other_equipment_cannot_set_sample_status(lab, who, message, status, monkeypatch):
    from iic_booking.equipment import api_views

    refunds = []
    monkeypatch.setattr(api_views, "refund_booking_internal", lambda booking, notes, user: refunds.append(booking.pk))

    resp = _set_status(lab, getattr(lab, who), status, reason="Wrong container")

    assert resp.status_code == 403
    assert message in resp.data["error"]
    assert not BookingSampleTrace.objects.filter(booking=lab.booking).exists()
    assert refunds == []


@pytest.mark.django_db
def test_admin_accepts_sample_on_any_equipment(lab):
    resp = _set_status(lab, lab.admin, SampleTraceStatus.SAMPLE_ACCEPTED)

    assert resp.status_code == 201, resp.content


@pytest.mark.django_db
def test_active_oic_substitute_accepts_sample(lab):
    EquipmentTemporaryOIC.objects.create(
        equipment=lab.eq, primary_oic=lab.oic, temporary_oic=lab.other_oic, resume_at=timezone.now() + timedelta(days=2)
    )

    resp = _set_status(lab, lab.other_oic, SampleTraceStatus.SAMPLE_ACCEPTED)

    assert resp.status_code == 201, resp.content


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
