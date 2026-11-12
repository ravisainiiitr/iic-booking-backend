"""No refund of a charge-recalculation difference on a Completed booking or one settled during a maintenance hold."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import BookingEvent, BookingEventType, BookingStatus, EquipmentManager
from iic_booking.equipment.maintenance_settlement import SETTLED_MARKER
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory


def _booking_with_pending_refund(f, *, status=BookingStatus.BOOKED):
    eq = f.equipment(enable_charge_recalculation=True)
    owner = f.student()
    booking = f.booking(owner, eq, f.future(), total_charge="10.00")
    faculty = UserFactory(user_type=UserType.FACULTY, department=f.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(student=owner, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED)
    SubWalletRepository.get_or_create(wallet, f.department).credit(Decimal("1000.00"), description="Recharge")
    booking.status = status
    booking.charge_recalculation_pending_amount = Decimal("-40.00")
    if status == BookingStatus.COMPLETED:
        booking.completed_at = timezone.now()
    booking.save()
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return booking, oic


def _confirm(f, user, booking):
    return f.client_for(user).post(
        f"/api/bookings/{booking.pk}/process-charge-recalculation-refund/", {}, format="json"
    )


@pytest.mark.django_db
def test_completed_booking_recalc_refund_refused(egs_factory):
    booking, oic = _booking_with_pending_refund(egs_factory, status=BookingStatus.COMPLETED)
    res = _confirm(egs_factory, oic, booking)
    assert res.status_code == 400
    assert "Completed" in res.json()["error"]
    booking.refresh_from_db()
    assert booking.charge_recalculation_pending_amount == Decimal("-40.00")


@pytest.mark.django_db
def test_settled_booking_recalc_refund_refused(egs_factory):
    booking, oic = _booking_with_pending_refund(egs_factory, status=BookingStatus.BOOKING_NOT_UTILIZED)
    BookingEvent.objects.create(
        booking=booking, event_type=BookingEventType.STATUS_CHANGED, comment="settled",
        metadata={SETTLED_MARKER: True},
    )
    res = _confirm(egs_factory, oic, booking)
    assert res.status_code == 400
    assert "can no longer be refunded" in res.json()["error"]
    booking.refresh_from_db()
    assert booking.charge_recalculation_pending_amount == Decimal("-40.00")


@pytest.mark.django_db
def test_booked_booking_recalc_refund_still_paid(egs_factory):
    booking, oic = _booking_with_pending_refund(egs_factory)
    res = _confirm(egs_factory, oic, booking)
    assert res.status_code == 200, res.content
    booking.refresh_from_db()
    assert booking.charge_recalculation_pending_amount is None
