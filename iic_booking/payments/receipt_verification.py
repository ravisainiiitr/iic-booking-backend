"""Checking that a payment receipt is genuine (9 Nov 2026).

Every receipt carries a **verification code** and a QR code that opens the public check page
``<site>/verify-receipt?n=<receipt number>&c=<code>``. The page asks the server, which looks the receipt up in its own
records and answers with what the portal holds: amount (base and GST), date, payer, purpose, department and grant
code, booking, and whether the payment or booking has since been refunded or cancelled.

The code is a keyed hash (HMAC-SHA256) of the payment's id, receipt number and amount, made with a secret that only the
server knows (``RECEIPT_VERIFY_SECRET``, else the site's ``SECRET_KEY``). It cannot be worked out from what is printed,
so a made-up or altered receipt has no code that checks out. Nothing is stored: the code is computed again on each
check. Changing the secret changes every code, so it must stay fixed once receipts are issued.
"""

from __future__ import annotations

import base64
import hmac
from urllib.parse import urlencode

from django.conf import settings
from django.utils.crypto import salted_hmac

CODE_LENGTH = 10


def _secret() -> str | None:
    return (getattr(settings, "RECEIPT_VERIFY_SECRET", "") or "").strip() or None


def verification_code(payment) -> str:
    """e.g. "K7Q2M-9XWPD": ten characters from the keyed hash, in two groups of five."""
    message = f"{payment.pk}|{payment.receipt_number or ''}|{payment.amount}"
    digest = salted_hmac("iic.payments.receipt-verify", message, secret=_secret(), algorithm="sha256").digest()
    raw = base64.b32encode(digest).decode("ascii").rstrip("=")[:CODE_LENGTH]
    return f"{raw[:5]}-{raw[5:]}"


def _normalise_code(code: str) -> str:
    raw = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    # Base32 has no 0, 1 or 8: read them as the letters people mistake them for.
    raw = raw.replace("0", "O").replace("1", "I").replace("8", "B")
    return f"{raw[:5]}-{raw[5:]}" if len(raw) == CODE_LENGTH else ""


def verify_url(payment) -> str:
    from iic_booking.equipment.equipment_qr import public_base_url

    query = urlencode({"n": payment.receipt_number or "", "c": verification_code(payment)})
    return f"{public_base_url()}/verify-receipt?{query}"


def _status(payment) -> tuple[str, str]:
    """(state, plain words) for the check page."""
    from .models import PaymentStatus

    if payment.status == PaymentStatus.REFUNDED:
        return "refunded", "Refunded in full"
    if payment.status == PaymentStatus.PARTIALLY_REFUNDED:
        return "partly_refunded", "Partly refunded"
    if payment.status != PaymentStatus.CAPTURED:
        return "other", payment.get_status_display()
    return "paid", "Paid"


# Booking statuses after which the money may have gone back (to the wallet or a bank): the receipt still checks out,
# but the page says so plainly so it is not accepted as if the service had been delivered.
UNDONE_BOOKING_STATUSES = {"CANCELLED", "REFUNDED"}


def _booking_of(order):
    booking = order.booking
    if booking is None:
        checkout = getattr(order, "external_checkout", None)
        booking = getattr(checkout, "booking", None)
    return booking


def _booking_note(order) -> str:
    booking = _booking_of(order)
    if booking is None:
        return ""
    status = str(getattr(booking, "status", "") or "")
    if status.upper() in UNDONE_BOOKING_STATUSES:
        return (
            f"The booking was {booking.get_status_display().lower()}. Any refund goes to the payer's portal wallet; "
            "check with the department's Accounts In Charge before accepting this receipt for reimbursement."
        )
    return f"Booking status: {booking.get_status_display()}."


def check(receipt_number: str, code: str) -> dict:
    """What the check page shows. ``{"valid": False}`` for an unknown number or a code that does not match."""
    from .models import Payment
    from .receipts import receipt_for

    number = " ".join(str(receipt_number or "").split()).upper()
    wanted = _normalise_code(code)
    if not number or not wanted:
        return {"valid": False}
    payment = (
        Payment.objects.select_related("payment_order", "payment_order__user", "payment_order__department",
                                       "payment_order__booking")
        .filter(receipt_number__iexact=number)
        .first()
    )
    if payment is None or not hmac.compare_digest(verification_code(payment), wanted):
        return {"valid": False}
    r = receipt_for(payment)
    state, state_label = _status(payment)
    return {
        "valid": True,
        "receipt_number": payment.receipt_number,
        "test_mode": r["test_mode"],
        "paid_at": r["paid_at"],
        "currency": r["currency"],
        "total": r["total_paid"],
        # The booking's own charge, split into base and GST (blank for a wallet recharge, which has no GST).
        "booking_total": r.get("booking_total") or "",
        "booking_gst": r.get("booking_gst") or "",
        "status": state,
        "status_label": state_label,
        "booking_note": _booking_note(payment.payment_order),
        "payer_name": r["payer_name"],
        "payer_organisation": r["payer_organisation"],
        "purpose": r["description"],
        "booking_reference": r["booking_reference"],
        "department_name": r.get("department_name", ""),
        "grant_code": r.get("grant_code", ""),
        "collected_by": f"{r['collected_by']}, {r['institution']}",
        "razorpay_payment_id": r["razorpay_payment_id"],
        # The bank's own reference for the payment (UPI RRN / net-banking UTR), and the UTR of the transfer in which
        # Razorpay paid the institute: each blank until known.
        "bank_reference": r.get("bank_reference") or "",
        "settlement_utr": r["settlement_utr"],
        # The booking's status now (Booked, Completed, Cancelled, Refunded ...), so a later cancellation shows.
        **_booking_status(payment.payment_order),
    }


def _booking_status(order) -> dict:
    booking = _booking_of(order)
    if booking is None:
        return {"booking_status": "", "booking_status_label": "", "booking_undone": False}
    code = str(getattr(booking, "status", "") or "").upper()
    return {"booking_status": code, "booking_status_label": booking.get_status_display(),
            "booking_undone": code in UNDONE_BOOKING_STATUSES}
