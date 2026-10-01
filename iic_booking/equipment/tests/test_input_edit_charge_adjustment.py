"""User input edits: OIC may edit after completion; refunds of a lower recalculated charge need OIC confirmation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import (
    BookingStatus,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _setup(egs_factory, *, status=BookingStatus.BOOKED):
    eq = egs_factory.equipment(enable_charge_recalculation=True)
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=True,
    )
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 2}, total_charge="50.00")
    if status == BookingStatus.COMPLETED:
        booking.status = status
        booking.completed_at = timezone.now()
        booking.save(update_fields=["status", "completed_at"])
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return eq, owner, oic, booking


def _patch(egs_factory, user, booking, values):
    return egs_factory.client_for(user).patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": values}, format="json"
    )


@pytest.mark.django_db
def test_user_cannot_edit_completed_booking_but_oic_can_and_charge_is_recalculated(egs_factory):
    _eq, owner, oic, booking = _setup(egs_factory, status=BookingStatus.COMPLETED)

    assert _patch(egs_factory, owner, booking, {"A": 3}).status_code == 400

    resp = _patch(egs_factory, oic, booking, {"A": 3})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 3
    assert booking.total_charge == Decimal("10.00")
    assert booking.charge_recalculation_pending_amount == Decimal("-40.00")


@pytest.mark.django_db
def test_refund_after_input_edit_requires_oic(egs_factory):
    _eq, owner, oic, booking = _setup(egs_factory)

    resp = _patch(egs_factory, owner, booking, {"A": 1})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.charge_recalculation_pending_amount < 0

    url = f"/api/bookings/{booking.pk}/process-charge-recalculation-refund/"
    denied = egs_factory.client_for(owner).post(url, {}, format="json")
    assert denied.status_code == 403
    allowed = egs_factory.client_for(oic).post(url, {}, format="json")
    assert allowed.status_code != 403
