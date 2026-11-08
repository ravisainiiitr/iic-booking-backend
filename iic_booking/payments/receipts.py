"""Payment receipt data for an online (Razorpay) payment.

The receipt number is the order's existing receipt reference (``PaymentOrder.receipt``, also sent to Razorpay as the
order's receipt), so no second numbering scheme is introduced. A receipt is never a GST tax invoice: where a tax
invoice applies, SRIC issues it.
"""

from __future__ import annotations

from django.conf import settings

from .models import Payment
from .models import PaymentOrderStatus
from .models import PaymentPurpose
from .models import PaymentStatus
from .purpose import booking_purpose
from .purpose import profile_type_of
from .purpose import recharge_purpose

RECEIPT_STATUSES = (PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED)


def checkout_name() -> str:
    """The merchant name shown in Razorpay Checkout and on receipts. Must match the Razorpay account's business name."""
    return str(getattr(settings, "RAZORPAY_CHECKOUT_NAME", "") or "DEAN SRIC IIT ROORKEE").strip()


def _service(order) -> dict:
    booking = order.booking
    checkout = getattr(order, "external_checkout", None) if order.booking_id is None else None
    if booking is None and checkout is not None:
        booking = checkout.booking
    if order.purpose == PaymentPurpose.WALLET_RECHARGE:
        department = getattr(order.department, "name", "") or "department"
        return {"description": recharge_purpose(department), "booking_reference": "", "equipment": "",
                "booking_total": "", "booking_gst": ""}
    equipment = getattr(getattr(booking, "equipment", None), "name", "") or getattr(getattr(checkout, "equipment", None), "name", "")
    reference = (getattr(booking, "virtual_booking_id", "") or str(getattr(booking, "booking_id", "") or "")) if booking else ""
    gst = None
    if booking is not None:
        from iic_booking.external_services.refunds import _gst_in_breakdown

        gst = _gst_in_breakdown(booking)
    return {
        "description": booking_purpose(equipment, profile_type_of(booking, getattr(booking, "equipment", None) or getattr(checkout, "equipment", None))),
        "booking_reference": reference,
        "equipment": equipment,
        # The booking's own total and the GST inside it, from its charge breakdown (blank for wallet recharges).
        "booking_total": str(booking.total_charge) if booking is not None and booking.total_charge is not None else "",
        "booking_gst": str(gst) if gst else "",
    }


def institute_gstin() -> str:
    """The SRIC office GSTIN printed on its receipts (shown for reference; this receipt is not a tax invoice)."""
    return str(getattr(settings, "RECEIPT_INSTITUTE_GSTIN", "") or "").strip().upper()


def _payer_organisation(user) -> str:
    for attr in ("organisation", "organization", "institute_name", "company_name"):
        value = getattr(user, attr, "")
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    department = getattr(user, "department", None)
    return (getattr(department, "name", "") or "")[:200]


def receipt_for(payment: Payment) -> dict:
    from .instrument import describe

    order = payment.payment_order
    user = order.user
    how = describe(payment)
    return {
        "receipt_number": order.receipt,
        "title": "Payment Receipt",
        "not_a_tax_invoice": "This is a payment receipt, not a GST tax invoice. Where a tax invoice applies, it is issued by SRIC, IIT Roorkee.",
        "computer_generated": "This is a computer-generated receipt and does not require a signature.",
        "test_mode": order.mode == "test",
        "collected_by": checkout_name(),
        "institution": "Indian Institute of Technology Roorkee",
        "office": "Sponsored Research & Industrial Consultancy (SRIC)",
        "institute_gstin": institute_gstin(),
        "service_name": "IIT Roorkee Equipment Booking Portal",
        "payer_name": getattr(user, "name", "") or "",
        "payer_organisation": _payer_organisation(user),
        "payer_email": getattr(user, "email", "") or "",
        "purpose": order.get_purpose_display(),
        **_service(order),
        "razorpay_payment_id": payment.razorpay_payment_id,
        "razorpay_order_id": order.razorpay_order_id,
        "method": payment.method,
        # How the payment was made, with that mode's own references (UPI RRN, card authorisation code, bank txn no.).
        "payment_mode": how["mode"],
        "payment_details": how["details"],
        # The bank's own reference for the payment (UPI RRN / netbanking transaction no.) as Razorpay reports it, and the
        # UTR of the bank transfer in which Razorpay settled it to the institute. Blank until known; cards have no UTR.
        "bank_reference": payment.gateway_reference or "",
        "settlement_utr": _settlement_utr(payment),
        "paid_at": payment.created_at.isoformat(),
        "status": payment.get_status_display(),
        "currency": order.currency,
        "base_amount": str(order.base_amount),
        "convenience_fee": str(order.convenience_fee),
        "gst_on_fee": str(order.fee_gst),
        "gst_on_fee_percent": str(order.fee_gst_percent_snapshot),
        "total_paid": str(payment.amount),
    }


def _settlement_utr(payment: Payment) -> str:
    line = payment.settlement_lines.exclude(settlement_utr="").order_by("-settled_at").first()
    return line.settlement_utr if line else ""


def receipt_summary(payment: Payment) -> dict:
    """One line of a receipts list."""
    from .purpose import order_purpose

    order = payment.payment_order
    return {
        "razorpay_payment_id": payment.razorpay_payment_id,
        "receipt_number": order.receipt,
        "purpose": order_purpose(order),
        "total_paid": str(payment.amount),
        "paid_at": payment.created_at.isoformat(),
        "test_mode": order.mode == "test",
    }


def receipts_for(queryset) -> list[dict]:
    """Confirmed online payments, newest first."""
    rows = (
        queryset.filter(status__in=RECEIPT_STATUSES, payment_order__status=PaymentOrderStatus.PAID)
        .select_related("payment_order__department", "payment_order__booking__equipment")
        .order_by("-created_at")
    )
    out = []
    for payment in rows[:200]:
        try:
            out.append(receipt_summary(payment))
        except Exception:  # one odd row must never hide the others
            continue
    return out


def receipt_allowed(payment: Payment) -> bool:
    return payment.status in RECEIPT_STATUSES and payment.payment_order.status == PaymentOrderStatus.PAID
