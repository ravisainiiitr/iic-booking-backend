"""One-minute payment window for a user's own input edit that raises the booking charge.

When the booking user edits inputs and the recalculated charge goes up, the pre-edit state
(input values and charge) is stored on the booking together with a pay deadline. Paying the extra
amount before the deadline keeps the edit; otherwise the booking is restored to the stored state and
the edit is cancelled (lazily on read/edit/pay, by ``equipment.expire_unpaid_input_edits`` and by the
explicit cancel endpoint).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

INPUT_EDIT_PAYMENT_WINDOW_SECONDS = 60
# Tolerates request latency for a Pay click made as the visible countdown reaches zero.
INPUT_EDIT_PAYMENT_GRACE_SECONDS = 5

WINDOW_FIELDS = ["charge_recalculation_pay_deadline", "charge_recalculation_revert_snapshot"]


def snapshot_booking_charge_state(booking) -> Dict[str, Any]:
    pending = booking.charge_recalculation_pending_amount
    return {
        "input_values": dict(booking.input_values or {}),
        "total_charge": str(booking.total_charge if booking.total_charge is not None else Decimal("0.00")),
        "total_time_minutes": int(booking.total_time_minutes or 0),
        "charge_breakdown": list(booking.charge_breakdown or []),
        "pending_amount": str(pending) if pending is not None else None,
    }


def new_payment_deadline(now=None):
    return (now or timezone.now()) + timedelta(seconds=INPUT_EDIT_PAYMENT_WINDOW_SECONDS)


def has_payment_window(booking) -> bool:
    return bool(booking.charge_recalculation_pay_deadline and booking.charge_recalculation_revert_snapshot)


def payment_window_expired(booking, now=None) -> bool:
    if not has_payment_window(booking):
        return False
    cutoff = booking.charge_recalculation_pay_deadline + timedelta(seconds=INPUT_EDIT_PAYMENT_GRACE_SECONDS)
    return (now or timezone.now()) > cutoff


def payment_seconds_remaining(booking, now=None) -> Optional[int]:
    if not has_payment_window(booking):
        return None
    remaining = (booking.charge_recalculation_pay_deadline - (now or timezone.now())).total_seconds()
    return max(0, int(remaining + 0.999))


def clear_payment_window(booking) -> list:
    booking.charge_recalculation_pay_deadline = None
    booking.charge_recalculation_revert_snapshot = None
    return list(WINDOW_FIELDS)


def revert_unpaid_input_edit(booking_pk, *, actor=None, reason: str = "expired"):
    """Restore the pre-edit inputs and charge of a booking with an unpaid edit.

    ``reason`` is ``"expired"`` (deadline passed) or ``"cancelled"`` (user/staff cancelled the edit).
    Returns the reverted booking, or None when there was nothing to revert.
    """
    from .booking_events import create_booking_event
    from .models import Booking, BookingEventType

    with transaction.atomic():
        booking = Booking.objects.select_for_update().filter(pk=booking_pk).first()
        if booking is None or not has_payment_window(booking):
            return None
        if reason == "expired" and not payment_window_expired(booking):
            return None
        snapshot = booking.charge_recalculation_revert_snapshot or {}
        pending = booking.charge_recalculation_pending_amount
        if pending is None or pending <= 0:
            booking.save(update_fields=clear_payment_window(booking))
            return None

        unpaid_charge = booking.total_charge
        restored_pending = snapshot.get("pending_amount")
        booking.input_values = snapshot.get("input_values") or {}
        booking.total_charge = Decimal(str(snapshot.get("total_charge") or "0"))
        booking.total_time_minutes = int(snapshot.get("total_time_minutes") or 0)
        booking.charge_breakdown = snapshot.get("charge_breakdown") or []
        booking.charge_recalculation_pending_amount = (
            Decimal(str(restored_pending)) if restored_pending is not None else None
        )
        update_fields = [
            "input_values",
            "total_charge",
            "total_time_minutes",
            "charge_breakdown",
            "charge_recalculation_pending_amount",
        ] + clear_payment_window(booking)
        booking.save(update_fields=update_fields)

        if reason == "expired":
            comment = (
                f"Edit cancelled: the additional ₹{pending:.2f} was not paid within "
                f"{INPUT_EDIT_PAYMENT_WINDOW_SECONDS} seconds. Previous inputs and charge "
                f"(₹{booking.total_charge:.2f}) have been restored."
            )
        else:
            comment = (
                f"Edit cancelled before paying the additional ₹{pending:.2f}. Previous inputs and charge "
                f"(₹{booking.total_charge:.2f}) have been restored."
            )
        try:
            create_booking_event(
                booking=booking,
                event_type=BookingEventType.CHARGE_RECALCULATED,
                created_by=actor,
                comment=comment,
                metadata={
                    "previous_charge": str(unpaid_charge),
                    "new_charge": str(booking.total_charge),
                    "charge_breakdown": booking.charge_breakdown,
                    "input_edit_reverted": True,
                    "revert_reason": reason,
                },
                send_notification=True,
            )
        except Exception:
            logger.exception("Failed to log input edit revert for booking %s", booking.pk)
    return booking


def expire_unpaid_input_edit(booking) -> bool:
    """Revert ``booking`` in place when its payment window is over. Returns True if reverted."""
    if not payment_window_expired(booking):
        return False
    reverted = revert_unpaid_input_edit(booking.pk, reason="expired")
    booking.refresh_from_db()
    return reverted is not None


def expire_unpaid_input_edits(queryset=None, now=None) -> int:
    """Revert every booking (optionally within ``queryset``) whose payment window is over."""
    from .models import Booking

    cutoff = (now or timezone.now()) - timedelta(seconds=INPUT_EDIT_PAYMENT_GRACE_SECONDS)
    qs = queryset if queryset is not None else Booking.objects.all()
    overdue_ids = list(
        qs.filter(
            charge_recalculation_pay_deadline__isnull=False,
            charge_recalculation_pay_deadline__lt=cutoff,
        ).values_list("pk", flat=True)[:500]
    )
    reverted = 0
    for pk in overdue_ids:
        try:
            if revert_unpaid_input_edit(pk, reason="expired") is not None:
                reverted += 1
        except Exception:
            logger.exception("Failed to revert unpaid input edit for booking %s", pk)
    return reverted
