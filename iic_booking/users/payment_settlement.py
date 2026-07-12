"""Settle SBIePay transactions: credit wallet or confirm booking."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, IstemFbrStatus, charge_profile_requires_istem_fbr, initial_istem_fbr_fields_for_charge_profile
from iic_booking.users.models.payment import (
    PaymentGatewayStatus,
    PaymentGatewayTransaction,
    PaymentPurpose,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.repositories.wallet_repository import SubWalletRepository

logger = logging.getLogger(__name__)


@transaction.atomic
def settle_payment_gateway_transaction(txn: PaymentGatewayTransaction) -> PaymentGatewayTransaction:
    txn = PaymentGatewayTransaction.objects.select_for_update().get(pk=txn.pk)
    if txn.status == PaymentGatewayStatus.SUCCESS:
        return txn

    if txn.purpose == PaymentPurpose.WALLET_RECHARGE:
        if not txn.wallet_id:
            raise ValueError("Wallet missing on recharge transaction")
        sub = SubWalletRepository.get_or_create(txn.wallet, txn.department)
        desc = f"SBIePay recharge — {txn.merchant_order_ref}"
        sub.credit(txn.amount, desc, related_user=txn.user)

    elif txn.purpose == PaymentPurpose.BOOKING_SHORTFALL:
        if not txn.booking_id:
            raise ValueError("Booking missing on shortfall transaction")
        booking = Booking.objects.select_for_update().get(pk=txn.booking_id)
        due = Decimal(str(booking.amount_due or 0))
        if due > 0 and txn.amount >= due:
            booking.payment_settled_at = timezone.now()
            if booking.status == BookingStatus.PENDING_PAYMENT:
                booking.status = BookingStatus.BOOKED
            if not booking.istem_fbr_status and charge_profile_requires_istem_fbr(booking.charge_profile):
                fbr_defaults = initial_istem_fbr_fields_for_charge_profile(booking.charge_profile)
                for k, v in fbr_defaults.items():
                    setattr(booking, k, v)
            booking.save(
                update_fields=[
                    "payment_settled_at",
                    "status",
                    "istem_fbr_status",
                    "updated_at",
                ]
            )

    txn.status = PaymentGatewayStatus.SUCCESS
    txn.verified_at = timezone.now()
    txn.save(update_fields=["status", "verified_at", "updated_at"])
    return txn
