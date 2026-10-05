"""Amount actually paid for a booking while a charge-recalculation difference is still open.

``Booking.total_charge`` is the current (recalculated) charge. Until the difference is settled,
``charge_recalculation_pending_amount`` holds it: negative = refund still waiting for the Officer In
Charge (the money is still held), positive = extra amount not yet collected. So the holder has paid
``total_charge - pending``. Refunds must use that figure, never ``total_charge`` alone.
"""

from __future__ import annotations

from decimal import Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _money(value) -> Decimal:
    return Decimal(str(value if value is not None else "0")).quantize(CENT)


def pending_charge_difference(booking) -> Decimal | None:
    pending = getattr(booking, "charge_recalculation_pending_amount", None)
    if pending is None:
        return None
    pending = _money(pending)
    return pending if pending != ZERO else None


def booking_paid_charge(booking) -> Decimal:
    """What the booking holder has paid towards the current charge."""
    total = _money(getattr(booking, "total_charge", None))
    pending = pending_charge_difference(booking)
    if pending is None:
        return total
    return max(ZERO, total - pending)


def clear_pending_charge_difference(booking) -> list[str]:
    """Close the open difference (and any unpaid-edit payment window) after a full refund.

    Sets the attributes on ``booking`` and returns the fields to save.
    """
    from .input_edit_payment_window import clear_payment_window

    booking.charge_recalculation_pending_amount = None
    return ["charge_recalculation_pending_amount"] + clear_payment_window(booking)


def drop_unpaid_extra(booking) -> list[str]:
    """Forget an extra amount that was never collected (booking cancelled without refund).

    A refund still waiting for the Officer In Charge is kept: the overcharge can still be returned.
    """
    pending = pending_charge_difference(booking)
    if pending is None or pending < ZERO:
        return []
    return clear_pending_charge_difference(booking)


def net_refund_against_unpaid_extra(booking, refund_amount) -> tuple[Decimal, Decimal | None]:
    """Partial cancellation: settle an uncollected extra amount from the refund first.

    Returns ``(refund_to_credit, pending_after)``. A refund still waiting for the Officer In Charge is
    left as it is (it stays relative to the new, lower charge).
    """
    refund = max(ZERO, _money(refund_amount))
    pending = pending_charge_difference(booking)
    if pending is None or pending < ZERO:
        return refund, pending
    offset = min(refund, pending)
    remaining_extra = pending - offset
    return refund - offset, (remaining_extra if remaining_extra > ZERO else None)
