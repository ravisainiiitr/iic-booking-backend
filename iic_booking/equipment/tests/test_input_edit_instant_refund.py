"""A lower charge from the booking user's own input edit: refunded at once before the cancel / reschedule
cut-off (``equipment.reschedule_hours_threshold`` before the first slot), OIC-confirmed after it."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.input_edit_refund_window import instant_refund_window
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
)
from iic_booking.equipment.serializers import BookingSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWalletTransaction,
    Wallet,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory


def _setup(egs_factory, *, start=None, recalc_flag=False, **equipment_fields):
    # HOUR profile, ₹10/hour, A hours: A=1 costs ₹10, A=2 ₹20, A=5 ₹50.
    eq = egs_factory.equipment(time_formula="A*60", enable_charge_recalculation=recalc_flag, **equipment_fields)
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=False,
    )
    owner = egs_factory.student()
    booking = egs_factory.booking(
        owner, eq, start or egs_factory.future(days=3), input_values={"A": 2}, total_charge="20.00"
    )
    booking.total_time_minutes = 120
    booking.save(update_fields=["total_time_minutes"])

    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=owner, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    sub = SubWalletRepository.get_or_create(wallet, egs_factory.department)
    sub.credit(Decimal("1000.00"), description="Recharge")

    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return eq, owner, oic, booking, sub


def _patch(egs_factory, user, booking, values):
    return egs_factory.client_for(user).patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": values}, format="json"
    )


def _pay(egs_factory, user, booking):
    return egs_factory.client_for(user).post(
        f"/api/bookings/{booking.pk}/process-charge-recalculation-pay-now/", {}, format="json"
    )


def _confirm_refund(egs_factory, user, booking):
    return egs_factory.client_for(user).post(
        f"/api/bookings/{booking.pk}/process-charge-recalculation-refund/", {}, format="json"
    )


def _refund_credits(sub):
    return SubWalletTransaction.objects.filter(
        sub_wallet=sub,
        transaction_type=SubWalletTransaction.TransactionType.CREDIT,
        description__startswith="Refund for",
    )


@pytest.mark.django_db
def test_lower_charge_before_cutoff_is_refunded_at_once_to_the_booking_wallet(egs_factory):
    _eq, owner, oic, booking, sub = _setup(egs_factory)

    resp = _patch(egs_factory, owner, booking, {"A": 1})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    sub.refresh_from_db()

    assert booking.total_charge == Decimal("10.00")
    assert booking.charge_recalculation_pending_amount is None
    assert sub.balance == Decimal("1010.00")
    credits = list(_refund_credits(sub))
    assert len(credits) == 1
    assert credits[0].amount == Decimal("10.00")
    assert credits[0].related_user_id == owner.pk

    summary = resp.data["charge_recalculation_summary"]
    assert Decimal(summary["refund_amount"]) == Decimal("10")
    assert summary["refund_status"] == "refunded"
    assert "refunded to your wallet" in resp.data["message"]

    event = (
        BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.CHARGE_RECALCULATED)
        .order_by("-event_id")
        .first()
    )
    assert event.metadata["refund_status"] == "refunded"
    assert event.metadata["wallet_transaction_id"] == credits[0].pk
    assert "has been refunded to the wallet" in event.comment

    # Nothing is left for the Officer In Charge to confirm, so no second refund is possible.
    assert _confirm_refund(egs_factory, oic, booking).status_code == 400
    sub.refresh_from_db()
    assert sub.balance == Decimal("1010.00")


@pytest.mark.django_db
def test_lower_charge_after_cutoff_waits_for_oic_confirmation(egs_factory):
    _eq, owner, oic, booking, sub = _setup(egs_factory, start=egs_factory.future(days=1))

    resp = _patch(egs_factory, owner, booking, {"A": 1})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.charge_recalculation_pending_amount == Decimal("-10.00")
    assert sub.balance == Decimal("1000.00")
    assert not _refund_credits(sub).exists()
    assert resp.data["charge_recalculation_summary"]["refund_status"] == "awaiting_oic_confirmation"
    assert "after the Officer In Charge approves" in resp.data["message"]

    assert _confirm_refund(egs_factory, owner, booking).status_code == 403
    assert _confirm_refund(egs_factory, oic, booking).status_code == 200
    sub.refresh_from_db()
    assert sub.balance == Decimal("1010.00")
    assert _confirm_refund(egs_factory, oic, booking).status_code == 400


@pytest.mark.django_db
def test_higher_charge_before_cutoff_still_opens_the_pay_window(egs_factory):
    _eq, owner, _oic, booking, sub = _setup(egs_factory)

    resp = _patch(egs_factory, owner, booking, {"A": 5})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.charge_recalculation_pending_amount == Decimal("30.00")
    assert booking.charge_recalculation_pay_deadline is not None
    assert resp.data["charge_recalculation_summary"]["refund_status"] is None
    assert sub.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_equal_charge_gives_no_refund(egs_factory):
    _eq, owner, _oic, booking, sub = _setup(egs_factory, recalc_flag=True)

    resp = _patch(egs_factory, owner, booking, {"A": 2, "comments": "same samples"})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.total_charge == Decimal("20.00")
    assert booking.charge_recalculation_pending_amount is None
    assert sub.balance == Decimal("1000.00")
    assert not _refund_credits(sub).exists()


@pytest.mark.django_db
def test_repeated_edits_net_against_what_was_paid(egs_factory):
    _eq, owner, _oic, booking, sub = _setup(egs_factory)

    assert _patch(egs_factory, owner, booking, {"A": 5}).status_code == 200
    assert _pay(egs_factory, owner, booking).status_code == 200  # paid ₹20 at booking + ₹30 extra = ₹50
    assert _patch(egs_factory, owner, booking, {"A": 3}).status_code == 200  # ₹30: refund ₹20
    assert _patch(egs_factory, owner, booking, {"A": 1}).status_code == 200  # ₹10: refund ₹20

    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.total_charge == Decimal("10.00")
    assert booking.charge_recalculation_pending_amount is None
    assert sorted(t.amount for t in _refund_credits(sub)) == [Decimal("20.00"), Decimal("20.00")]
    # 1000 - 30 extra + 20 + 20 refunds: the user has paid exactly the final ₹10 charge.
    assert sub.balance == Decimal("1010.00")


@pytest.mark.django_db
def test_refund_never_exceeds_the_amount_paid_after_an_unpaid_higher_edit(egs_factory):
    _eq, owner, _oic, booking, sub = _setup(egs_factory)

    assert _patch(egs_factory, owner, booking, {"A": 5}).status_code == 200  # ₹50, extra ₹30 not paid
    resp = _patch(egs_factory, owner, booking, {"A": 1})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    sub.refresh_from_db()

    # Refund is against the ₹20 actually paid, not the unpaid ₹50.
    assert Decimal(resp.data["charge_recalculation_summary"]["previous_charge"]) == Decimal("20")
    assert [t.amount for t in _refund_credits(sub)] == [Decimal("10.00")]
    assert sub.balance == Decimal("1010.00")
    assert booking.charge_recalculation_pending_amount is None
    assert booking.charge_recalculation_pay_deadline is None
    assert booking.charge_recalculation_revert_snapshot is None


@pytest.mark.django_db
def test_unpaid_balance_keeps_the_refund_with_the_oic(egs_factory):
    _eq, owner, _oic, booking, sub = _setup(egs_factory)
    booking.amount_due = Decimal("20.00")
    booking.save(update_fields=["amount_due"])

    assert _patch(egs_factory, owner, booking, {"A": 1}).status_code == 200
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.charge_recalculation_pending_amount == Decimal("-10.00")
    assert sub.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_oic_edit_before_cutoff_keeps_the_confirm_refund_step(egs_factory):
    _eq, _owner, oic, booking, sub = _setup(egs_factory)

    assert _patch(egs_factory, oic, booking, {"A": 1}).status_code == 200
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.charge_recalculation_pending_amount == Decimal("-10.00")
    assert sub.balance == Decimal("1000.00")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("threshold", "days_ahead", "instant"),
    [
        (0, 3, True),  # 0 / not set: the default 48 hours applies, as for cancel / reschedule
        (0, 1, False),
        (100, 3, False),  # the equipment's own cut-off is used
    ],
)
def test_cutoff_uses_the_equipment_reschedule_threshold(egs_factory, threshold, days_ahead, instant):
    _eq, owner, _oic, booking, sub = _setup(
        egs_factory, start=egs_factory.future(days=days_ahead), reschedule_hours_threshold=threshold
    )

    assert _patch(egs_factory, owner, booking, {"A": 1}).status_code == 200
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert (booking.charge_recalculation_pending_amount is None) is instant
    assert sub.balance == (Decimal("1010.00") if instant else Decimal("1000.00"))


@pytest.mark.django_db
def test_serializer_exposes_the_refund_deadline(egs_factory):
    start = egs_factory.future(days=3)
    _eq, _owner, _oic, booking, _sub = _setup(egs_factory, start=start)

    data = BookingSerializer(booking).data
    assert data["input_edit_instant_refund_open"] is True
    deadline = datetime.fromisoformat(data["input_edit_refund_deadline"])
    assert deadline == start - timedelta(hours=48)


@pytest.mark.django_db
def test_window_helper_edge_cases(egs_factory):
    _eq, _owner, _oic, booking, _sub = _setup(egs_factory, start=egs_factory.future(days=1))
    assert instant_refund_window(booking)[0] is False

    booking.maintenance_disruption_flag = True
    assert instant_refund_window(booking) == (True, None)

    booking.maintenance_disruption_flag = False
    booking.daily_slots.all().delete()
    assert instant_refund_window(booking) == (False, None)

    _eq2, _owner2, _oic2, later, _sub2 = _setup(egs_factory)
    cutoff = instant_refund_window(later)[1]
    assert instant_refund_window(later, now=cutoff) == (True, cutoff)
    assert instant_refund_window(later, now=cutoff + timedelta(seconds=1))[0] is False
    assert cutoff < timezone.now() + timedelta(days=3)
