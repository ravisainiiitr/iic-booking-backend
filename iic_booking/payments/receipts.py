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
        return {"description": f"Wallet recharge, {department} sub-wallet", "booking_reference": "", "equipment": "",
                "booking_total": "", "booking_gst": ""}
    equipment = getattr(getattr(booking, "equipment", None), "name", "") or getattr(getattr(checkout, "equipment", None), "name", "")
    reference = (getattr(booking, "virtual_booking_id", "") or str(getattr(booking, "booking_id", "") or "")) if booking else ""
    gst = None
    if booking is not None:
        from iic_booking.external_services.refunds import _gst_in_breakdown

        gst = _gst_in_breakdown(booking)
    return {
        "description": f"Equipment booking charges{f', {equipment}' if equipment else ''}",
        "booking_reference": reference,
        "equipment": equipment,
        # The booking's own total and the GST inside it, from its charge breakdown (blank for wallet recharges).
        "booking_total": str(booking.total_charge) if booking is not None and booking.total_charge is not None else "",
        "booking_gst": str(gst) if gst else "",
    }


def receipt_for(payment: Payment) -> dict:
    order = payment.payment_order
    user = order.user
    return {
        "receipt_number": order.receipt,
        "title": "Payment Receipt",
        "not_a_tax_invoice": "This is a payment receipt, not a GST tax invoice. Where a tax invoice applies, it is issued by SRIC, IIT Roorkee.",
        "test_mode": order.mode == "test",
        "collected_by": checkout_name(),
        "institution": "Indian Institute of Technology Roorkee",
        "service_name": "IIT Roorkee Equipment Booking Portal",
        "payer_name": getattr(user, "name", "") or "",
        "purpose": order.get_purpose_display(),
        **_service(order),
        "razorpay_payment_id": payment.razorpay_payment_id,
        "razorpay_order_id": order.razorpay_order_id,
        "method": payment.method,
        "paid_at": payment.created_at.isoformat(),
        "status": payment.get_status_display(),
        "currency": order.currency,
        "base_amount": str(order.base_amount),
        "convenience_fee": str(order.convenience_fee),
        "gst_on_fee": str(order.fee_gst),
        "gst_on_fee_percent": str(order.fee_gst_percent_snapshot),
        "total_paid": str(payment.amount),
    }


def receipt_allowed(payment: Payment) -> bool:
    return payment.status in RECEIPT_STATUSES and payment.payment_order.status == PaymentOrderStatus.PAID
