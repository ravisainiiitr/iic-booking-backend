"""A user's input edit that raises the charge: real recalculated charge, one minute to pay, else revert."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.input_edit_payment_window import (
    INPUT_EDIT_PAYMENT_GRACE_SECONDS,
    INPUT_EDIT_PAYMENT_WINDOW_SECONDS,
    expire_unpaid_input_edits,
)
from iic_booking.equipment.models import DynamicInputField, DynamicInputFieldType, EquipmentManager
from iic_booking.equipment.serializers import BookingSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory


def _setup(egs_factory, *, recalc_flag=False, wallet_balance="1000.00"):
    # HOUR profile, ₹10/hour, A hours: A=2 costs ₹20, A=5 costs ₹50.
    eq = egs_factory.equipment(time_formula="A*60", enable_charge_recalculation=recalc_flag)
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=False,
    )
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 2}, total_charge="20.00")
    booking.total_time_minutes = 120
    booking.charge_breakdown = [{"description": "2 hours", "amount": 20.0}]
    booking.save(update_fields=["total_time_minutes", "charge_breakdown"])

    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=owner, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    sub = SubWalletRepository.get_or_create(wallet, egs_factory.department)
    sub.credit(Decimal(wallet_balance), description="Recharge")

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


def _expire(booking):
    booking.charge_recalculation_pay_deadline = timezone.now() - timedelta(
        seconds=INPUT_EDIT_PAYMENT_GRACE_SECONDS + 1
    )
    booking.save(update_fields=["charge_recalculation_pay_deadline"])


@pytest.mark.django_db
@pytest.mark.parametrize("recalc_flag", [False, True])
def test_edit_raising_charge_is_not_discounted_and_opens_one_minute_pay_window(egs_factory, recalc_flag):
    _eq, owner, _oic, booking, _sub = _setup(egs_factory, recalc_flag=recalc_flag)

    before = timezone.now()
    resp = _patch(egs_factory, owner, booking, {"A": 5})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()

    assert booking.input_values["A"] == 5
    assert booking.total_charge == Decimal("50.00")
    assert booking.charge_recalculation_pending_amount == Decimal("30.00")
    assert booking.charge_recalculation_revert_snapshot["input_values"] == {"A": 2}
    assert booking.charge_recalculation_revert_snapshot["total_charge"] == "20.00"
    window = booking.charge_recalculation_pay_deadline - before
    assert timedelta(seconds=INPUT_EDIT_PAYMENT_WINDOW_SECONDS - 5) <= window <= timedelta(
        seconds=INPUT_EDIT_PAYMENT_WINDOW_SECONDS + 5
    )

    data = BookingSerializer(booking).data
    lines = data["charge_breakdown"]
    assert all(Decimal(str(line["amount"])) >= 0 for line in lines)
    assert sum(Decimal(str(line["amount"])) for line in lines) == booking.total_charge
    assert 0 < data["charge_recalculation_pay_seconds_remaining"] <= INPUT_EDIT_PAYMENT_WINDOW_SECONDS
    summary = resp.data["charge_recalculation_summary"]
    assert summary["extra_amount"] == "30.00"
    assert summary["pay_window_seconds"] == INPUT_EDIT_PAYMENT_WINDOW_SECONDS


@pytest.mark.django_db
def test_paying_within_the_window_keeps_the_edit(egs_factory):
    _eq, owner, _oic, booking, sub = _setup(egs_factory)
    assert _patch(egs_factory, owner, booking, {"A": 5}).status_code == 200

    resp = _pay(egs_factory, owner, booking)
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.input_values["A"] == 5
    assert booking.total_charge == Decimal("50.00")
    assert booking.charge_recalculation_pending_amount is None
    assert booking.charge_recalculation_pay_deadline is None
    assert booking.charge_recalculation_revert_snapshot is None
    assert sub.balance == Decimal("970.00")

    assert expire_unpaid_input_edits() == 0
    booking.refresh_from_db()
    assert booking.input_values["A"] == 5


@pytest.mark.django_db
def test_after_the_window_pay_is_rejected_and_the_edit_is_reverted(egs_factory):
    _eq, owner, _oic, booking, sub = _setup(egs_factory)
    assert _patch(egs_factory, owner, booking, {"A": 5}).status_code == 200
    booking.refresh_from_db()
    _expire(booking)

    resp = _pay(egs_factory, owner, booking)
    assert resp.status_code == 400
    assert resp.data["code"] == "INPUT_EDIT_REVERTED"
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.input_values == {"A": 2}
    assert booking.total_charge == Decimal("20.00")
    assert booking.total_time_minutes == 120
    assert booking.charge_breakdown == [{"description": "2 hours", "amount": 20.0}]
    assert booking.charge_recalculation_pending_amount is None
    assert booking.charge_recalculation_pay_deadline is None
    assert booking.charge_recalculation_revert_snapshot is None
    assert sub.balance == Decimal("1000.00")

    assert _pay(egs_factory, owner, booking).status_code == 400


@pytest.mark.django_db
def test_periodic_task_and_reads_revert_expired_edits(egs_factory):
    _eq, owner, _oic, booking, _sub = _setup(egs_factory)
    assert _patch(egs_factory, owner, booking, {"A": 5}).status_code == 200
    booking.refresh_from_db()

    assert expire_unpaid_input_edits() == 0
    _expire(booking)
    assert expire_unpaid_input_edits() == 1
    booking.refresh_from_db()
    assert booking.input_values == {"A": 2}
    assert booking.total_charge == Decimal("20.00")

    assert _patch(egs_factory, owner, booking, {"A": 4}).status_code == 200
    booking.refresh_from_db()
    _expire(booking)
    listed = egs_factory.client_for(owner).get("/api/bookings/", {"booking_id": booking.pk, "limit": 1})
    assert listed.status_code == 200
    booking.refresh_from_db()
    assert booking.input_values == {"A": 2}
    assert booking.total_charge == Decimal("20.00")


@pytest.mark.django_db
def test_user_can_cancel_an_unpaid_edit(egs_factory):
    _eq, owner, _oic, booking, _sub = _setup(egs_factory)
    assert _patch(egs_factory, owner, booking, {"A": 5}).status_code == 200
    # A second edit within the window keeps the original values as the revert target.
    assert _patch(egs_factory, owner, booking, {"A": 6}).status_code == 200
    booking.refresh_from_db()
    assert booking.charge_recalculation_pending_amount == Decimal("40.00")

    url = f"/api/bookings/{booking.pk}/cancel-unpaid-input-edit/"
    resp = egs_factory.client_for(owner).post(url, {}, format="json")
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values == {"A": 2}
    assert booking.total_charge == Decimal("20.00")
    assert booking.charge_recalculation_pending_amount is None
    assert egs_factory.client_for(owner).post(url, {}, format="json").status_code == 400


@pytest.mark.django_db
def test_lower_charge_still_goes_to_oic_confirmed_refund_without_pay_window(egs_factory):
    _eq, owner, oic, booking, sub = _setup(egs_factory)

    resp = _patch(egs_factory, owner, booking, {"A": 1})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.total_charge == Decimal("10.00")
    assert booking.charge_recalculation_pending_amount == Decimal("-10.00")
    assert booking.charge_recalculation_pay_deadline is None
    assert booking.charge_recalculation_revert_snapshot is None

    url = f"/api/bookings/{booking.pk}/process-charge-recalculation-refund/"
    assert egs_factory.client_for(owner).post(url, {}, format="json").status_code == 403
    allowed = egs_factory.client_for(oic).post(url, {}, format="json")
    assert allowed.status_code == 200, allowed.data
    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.charge_recalculation_pending_amount is None
    assert sub.balance == Decimal("1010.00")


@pytest.mark.django_db
def test_oic_edit_raising_charge_has_no_pay_window(egs_factory):
    _eq, _owner, oic, booking, _sub = _setup(egs_factory)

    resp = _patch(egs_factory, oic, booking, {"A": 5})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.charge_recalculation_pending_amount == Decimal("30.00")
    assert booking.charge_recalculation_pay_deadline is None
    assert booking.charge_recalculation_revert_snapshot is None


@pytest.mark.django_db
def test_user_may_edit_fields_not_marked_editing_required(egs_factory):
    eq, owner, _oic, booking, _sub = _setup(egs_factory)
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="B",
        field_label="Select Element",
        field_type=DynamicInputFieldType.PERIODIC_TABLE,
        editing_required=False,
    )

    resp = _patch(egs_factory, owner, booking, {"A": 2, "B": 2, "B_elements": "Fe,Co"})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["B_elements"] == "Fe,Co"
    assert booking.input_values["B"] == 2
    keys = [f["field_key"] for f in BookingSerializer(booking).data["editable_input_fields"]]
    assert {"A", "B", "comments"} <= set(keys)
