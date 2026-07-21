"""Booking payment split: wallet first, remainder via SBIePay or offline UTR."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Tuple

from iic_booking.equipment.calculators import quantize_money
from iic_booking.users.models.user_type import UserType
from iic_booking.users.wallet_credit_facility import (
    subwallet_booking_balance_ok,
    wallet_booking_block_message,
    wallet_max_spendable_on_subwallet,
)


def compute_booking_payment_split(
    booking_target,
    total_charge,
    *,
    user_type: str,
    create_as_hold: bool,
) -> Tuple[Decimal, Decimal, Optional[str]]:
    """
    Returns (wallet_applied, amount_due, error_message).
    External users: apply available wallet, remainder collected separately.
    Internal users: full amount from wallet or error (unchanged behaviour).
    """
    tc = quantize_money(total_charge or 0)
    if create_as_hold or tc <= 0:
        return Decimal("0"), Decimal("0"), None

    block = wallet_booking_block_message(booking_target)
    if block:
        return Decimal("0"), tc, block

    if UserType.is_external_user(user_type or ""):
        spendable = wallet_max_spendable_on_subwallet(booking_target)
        applied = quantize_money(min(spendable, tc))
        due = quantize_money(tc - applied)
        return applied, due, None

    ok, err = subwallet_booking_balance_ok(booking_target, tc, create_as_hold)
    if not ok:
        return Decimal("0"), tc, err
    return tc, Decimal("0"), None


def booking_payment_fully_settled(booking) -> bool:
    due = Decimal(str(getattr(booking, "amount_due", None) or 0))
    return due <= 0 or getattr(booking, "payment_settled_at", None) is not None
