"""How the normal booking pipeline (``_book_equipment_impl``) runs for an external booking checkout.

The pipeline itself is unchanged for every other caller: these hooks only act while a checkout context is active
(``with active(ctx):``), which only ``checkout.py`` sets.

* ``QUOTE``: run every check and price the booking (slots locked and re-checked, quotas, limits, window), then stop
  before anything is written. No slot, wallet or booking changes.
* ``CONFIRM``: create the booking for exactly the quoted slots, as BOOKED, debiting ``wallet_amount`` (possibly 0) and
  leaving nothing due, because the rest was paid through the gateway. Refused when the price changed or the wallet can
  no longer cover ``wallet_amount``.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

QUOTE = "quote"
CONFIRM = "confirm"

_current: contextvars.ContextVar[Optional["CheckoutContext"]] = contextvars.ContextVar("external_checkout", default=None)


@dataclass
class CheckoutContext:
    mode: str
    expected_total: Optional[Decimal] = None
    wallet_amount: Decimal = Decimal("0.00")
    # Filled by the pipeline in QUOTE mode.
    quote: dict[str, Any] = field(default_factory=dict)


@contextlib.contextmanager
def active(ctx: CheckoutContext):
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def current() -> Optional[CheckoutContext]:
    return _current.get()


def payment_split(ctx: CheckoutContext, booking_target, total_charge) -> tuple[Decimal, Decimal, Optional[str]]:
    """``(wallet_applied, amount_due, error)`` in place of ``compute_booking_payment_split`` for a checkout."""
    from iic_booking.equipment.calculators import quantize_money
    from iic_booking.users.wallet_credit_facility import wallet_booking_block_message, wallet_max_spendable_on_subwallet

    total = quantize_money(total_charge or 0)
    if ctx.mode == QUOTE:
        return Decimal("0.00"), total, None
    if ctx.expected_total is None or total != quantize_money(ctx.expected_total):
        return Decimal("0.00"), total, "The booking charge has changed since it was priced."
    wallet = quantize_money(ctx.wallet_amount or 0)
    if wallet > 0:
        block = wallet_booking_block_message(booking_target)
        if block:
            return Decimal("0.00"), total, block
        if wallet > wallet_max_spendable_on_subwallet(booking_target):
            return Decimal("0.00"), total, "The wallet balance no longer covers the wallet part of this payment."
    return wallet, Decimal("0.00"), None
