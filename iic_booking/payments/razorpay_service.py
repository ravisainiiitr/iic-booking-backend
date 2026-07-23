"""Razorpay client, order creation, verify, webhook, settle, refund, settlements."""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from decimal import Decimal
from typing import Any

import razorpay
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    charge_profile_requires_istem_fbr,
    initial_istem_fbr_fields_for_charge_profile,
)
from iic_booking.payments.fees import compute_fee_breakup
from iic_booking.payments.models import (
    Payment,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentPurpose,
    PaymentRefund,
    PaymentSettlement,
    PaymentStatus,
    PaymentVerifiedVia,
    RefundStatus,
)
from iic_booking.users.repositories.wallet_repository import SubWalletRepository

logger = logging.getLogger(__name__)


class RazorpayNotConfigured(Exception):
    pass


class RazorpayServiceError(Exception):
    pass


def get_razorpay_client() -> razorpay.Client:
    key_id = getattr(settings, "RAZORPAY_KEY_ID", "") or ""
    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "") or ""
    if not key_id or not key_secret:
        raise RazorpayNotConfigured("Razorpay credentials are not configured.")
    return razorpay.Client(auth=(key_id, key_secret))


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "") or ""
    if not key_secret:
        raise RazorpayNotConfigured("Razorpay credentials are not configured.")
    message = f"{order_id}|{payment_id}"
    expected = hmac.new(
        key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(body: bytes | str, signature: str) -> bool:
    secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "") or ""
    if not secret:
        logger.warning("RAZORPAY_WEBHOOK_SECRET not configured; rejecting webhook")
        return False
    if isinstance(body, str):
        body = body.encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _amount_to_paise(amount: Decimal) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1")))


def _open_order_for_booking(booking: Booking) -> PaymentOrder | None:
    return (
        PaymentOrder.objects.filter(
            booking=booking,
            purpose=PaymentPurpose.BOOKING_SHORTFALL,
            status=PaymentOrderStatus.CREATED,
        )
        .order_by("-created_at")
        .first()
    )


@transaction.atomic
def create_order(
    *,
    user,
    purpose: str,
    base_amount: Decimal | None = None,
    booking: Booking | None = None,
    wallet=None,
    department=None,
) -> tuple[PaymentOrder, dict]:
    """
    Create PaymentOrder + Razorpay order. Recomputes fee server-side.
    For BOOKING_SHORTFALL, base is booking.amount_due (ignores client amount).
    """
    purpose = (purpose or "").strip().upper()
    if purpose not in PaymentPurpose.values:
        raise RazorpayServiceError("Invalid purpose.")

    if purpose == PaymentPurpose.BOOKING_SHORTFALL:
        if not booking:
            raise RazorpayServiceError("booking is required for booking shortfall.")
        booking = Booking.objects.select_for_update().get(pk=booking.pk)
        if booking.user_id != user.id:
            raise RazorpayServiceError("Forbidden.")
        due = Decimal(str(booking.amount_due or 0))
        if due <= 0 or booking.payment_settled_at:
            raise RazorpayServiceError("No balance due on this booking.")
        if booking.status not in (BookingStatus.PENDING_PAYMENT, BookingStatus.BOOKED):
            # Allow PENDING_PAYMENT primarily; BOOKED with due shouldn't happen
            if booking.status != BookingStatus.PENDING_PAYMENT:
                raise RazorpayServiceError("Booking is not awaiting payment.")
        base = due
        if department is None:
            department = booking.settlement_department
        existing = _open_order_for_booking(booking)
        if existing and existing.base_amount == base:
            # Reuse open order if still CREATED and amount unchanged
            key_id = getattr(settings, "RAZORPAY_KEY_ID", "") or ""
            return existing, {
                "order_id": existing.razorpay_order_id,
                "razorpay_order_id": existing.razorpay_order_id,
                "amount": _amount_to_paise(existing.total_amount),
                "currency": existing.currency,
                "key": key_id,
                "key_id": key_id,
                "payment_order_id": existing.id,
                "breakup": {
                    "base_amount": str(existing.base_amount),
                    "convenience_fee": str(existing.convenience_fee),
                    "fee_gst": str(existing.fee_gst),
                    "total_amount": str(existing.total_amount),
                    "fee_percent": str(existing.fee_percent_snapshot),
                    "fee_gst_percent": str(existing.fee_gst_percent_snapshot),
                },
                "purpose": existing.purpose,
                "booking_id": booking.booking_id,
            }
        if existing:
            existing.status = PaymentOrderStatus.CANCELLED
            existing.save(update_fields=["status", "updated_at"])

    elif purpose == PaymentPurpose.WALLET_RECHARGE:
        if wallet is None or department is None:
            raise RazorpayServiceError("wallet and department are required for recharge.")
        if base_amount is None:
            raise RazorpayServiceError("amount is required for recharge.")
        base = Decimal(str(base_amount)).quantize(Decimal("0.01"))
        if base <= 0:
            raise RazorpayServiceError("Amount must be positive.")
        if base > Decimal("100000"):
            raise RazorpayServiceError("Maximum recharge amount is ₹1,00,000.")
    else:
        raise RazorpayServiceError("Invalid purpose.")

    breakup = compute_fee_breakup(base)
    client = get_razorpay_client()
    receipt = f"iic_{purpose[:4].lower()}_{user.id}_{uuid.uuid4().hex[:10]}"
    idempotency_key = f"{purpose}:{user.id}:{booking.booking_id if booking else 0}:{department.id if department else 0}:{breakup.total_amount}:{uuid.uuid4().hex}"

    order_payload = {
        "amount": _amount_to_paise(breakup.total_amount),
        "currency": "INR",
        "receipt": receipt[:40],
        "notes": {
            "purpose": purpose,
            "user_id": str(user.id),
            "booking_id": str(booking.booking_id) if booking else "",
            "wallet_id": str(wallet.id) if wallet else "",
            "department_id": str(department.id) if department else "",
            "base_amount": str(breakup.base_amount),
        },
    }
    try:
        rp_order = client.order.create(data=order_payload)
    except Exception as e:
        logger.exception("Razorpay order.create failed")
        raise RazorpayServiceError(f"Failed to create payment order: {e}") from e

    payment_order = PaymentOrder.objects.create(
        razorpay_order_id=rp_order["id"],
        receipt=receipt[:64],
        purpose=purpose,
        user=user,
        booking=booking,
        wallet=wallet,
        department=department,
        base_amount=breakup.base_amount,
        convenience_fee=breakup.convenience_fee,
        fee_gst=breakup.fee_gst,
        total_amount=breakup.total_amount,
        currency="INR",
        status=PaymentOrderStatus.CREATED,
        idempotency_key=idempotency_key[:128],
        fee_percent_snapshot=breakup.fee_percent,
        fee_gst_percent_snapshot=breakup.fee_gst_percent,
        raw_create_response=rp_order if isinstance(rp_order, dict) else {"raw": str(rp_order)},
    )

    key_id = getattr(settings, "RAZORPAY_KEY_ID", "") or ""
    response = {
        "order_id": payment_order.razorpay_order_id,
        "razorpay_order_id": payment_order.razorpay_order_id,
        "amount": _amount_to_paise(payment_order.total_amount),
        "currency": payment_order.currency,
        "key": key_id,
        "key_id": key_id,
        "payment_order_id": payment_order.id,
        "breakup": breakup.as_dict(),
        "purpose": purpose,
        "booking_id": booking.booking_id if booking else None,
        "wallet_id": wallet.id if wallet else None,
        "department_id": department.id if department else None,
    }
    return payment_order, response


def _settle_booking_shortfall(order: PaymentOrder) -> None:
    if not order.booking_id:
        raise RazorpayServiceError("Booking missing on shortfall order")
    booking = Booking.objects.select_for_update().get(pk=order.booking_id)
    due = Decimal(str(booking.amount_due or 0))
    base = Decimal(str(order.base_amount or 0))
    if booking.payment_settled_at:
        return
    if due > 0 and base + Decimal("0.01") < due:
        # Paid less than due — do not confirm
        logger.error(
            "Razorpay settle base %s < booking due %s for booking %s",
            base,
            due,
            booking.booking_id,
        )
        raise RazorpayServiceError("Paid amount does not cover booking balance.")
    booking.payment_settled_at = timezone.now()
    booking.amount_due = Decimal("0.00")
    if booking.status == BookingStatus.PENDING_PAYMENT:
        booking.status = BookingStatus.BOOKED
    update_fields = ["payment_settled_at", "amount_due", "status", "updated_at"]
    if not booking.istem_fbr_status and charge_profile_requires_istem_fbr(booking.charge_profile):
        fbr_defaults = initial_istem_fbr_fields_for_charge_profile(booking.charge_profile)
        for k, v in fbr_defaults.items():
            setattr(booking, k, v)
        update_fields.append("istem_fbr_status")
    booking.save(update_fields=update_fields)


def _settle_wallet_recharge(order: PaymentOrder, payment: Payment) -> None:
    if not order.wallet_id or not order.department_id:
        raise RazorpayServiceError("Wallet/department missing on recharge order")
    sub = SubWalletRepository.get_or_create(order.wallet, order.department)
    desc = (
        f"Recharge via Razorpay — Order: {order.razorpay_order_id}, "
        f"Payment: {payment.razorpay_payment_id}"
    )
    # Credit base only (convenience fee is gateway cost borne by payer)
    sub.credit(order.base_amount, desc, related_user=order.user)


@transaction.atomic
def settle_order_success(
    *,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str = "",
    verified_via: str = PaymentVerifiedVia.CHECKOUT,
    raw_payload: dict | None = None,
    method: str = "",
    gateway_reference: str = "",
) -> tuple[PaymentOrder, Payment]:
    """
    Idempotent settlement: mark order PAID, create Payment, apply booking/wallet effects once.
    """
    order = (
        PaymentOrder.objects.select_for_update()
        .filter(razorpay_order_id=razorpay_order_id)
        .first()
    )
    if not order:
        raise RazorpayServiceError("Payment order not found.")

    existing_payment = Payment.objects.filter(razorpay_payment_id=razorpay_payment_id).first()
    if existing_payment and existing_payment.payment_order_id != order.id:
        raise RazorpayServiceError("Payment already linked to another order.")

    if order.status == PaymentOrderStatus.PAID:
        payment = existing_payment or order.payments.filter(status=PaymentStatus.CAPTURED).first()
        if payment:
            return order, payment

    if not existing_payment:
        payment = Payment.objects.create(
            payment_order=order,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature or "",
            method=method or "",
            gateway_reference=gateway_reference or "",
            amount=order.total_amount,
            status=PaymentStatus.CAPTURED,
            verified_via=verified_via,
            raw_payload=raw_payload or {},
        )
    else:
        payment = existing_payment

    # Apply side effects once when transitioning to PAID
    if order.status != PaymentOrderStatus.PAID:
        if order.purpose == PaymentPurpose.BOOKING_SHORTFALL:
            _settle_booking_shortfall(order)
        elif order.purpose == PaymentPurpose.WALLET_RECHARGE:
            _settle_wallet_recharge(order, payment)
        order.status = PaymentOrderStatus.PAID
        order.save(update_fields=["status", "updated_at"])

    return order, payment


def mark_order_failed(razorpay_order_id: str, raw_payload: dict | None = None) -> PaymentOrder | None:
    order = PaymentOrder.objects.filter(razorpay_order_id=razorpay_order_id).first()
    if not order:
        return None
    if order.status == PaymentOrderStatus.CREATED:
        order.status = PaymentOrderStatus.FAILED
        order.save(update_fields=["status", "updated_at"])
        logger.info(
            "Razorpay order %s marked FAILED (payload keys=%s)",
            razorpay_order_id,
            list((raw_payload or {}).keys()),
        )
    return order


def handle_webhook(payload: dict, body: bytes | str, signature: str) -> dict:
    if not verify_webhook_signature(body, signature):
        raise RazorpayServiceError("Invalid webhook signature.")

    event = (payload or {}).get("event") or ""
    payment_entity = (
        ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    )
    refund_entity = (
        ((payload.get("payload") or {}).get("refund") or {}).get("entity") or {}
    )
    settlement_entity = (
        ((payload.get("payload") or {}).get("settlement") or {}).get("entity") or {}
    )

    if event in ("payment.captured", "payment.authorized"):
        order_id = payment_entity.get("order_id")
        payment_id = payment_entity.get("id")
        if order_id and payment_id:
            settle_order_success(
                razorpay_order_id=order_id,
                razorpay_payment_id=payment_id,
                verified_via=PaymentVerifiedVia.WEBHOOK,
                raw_payload=payload,
                method=str(payment_entity.get("method") or ""),
                gateway_reference=str(
                    payment_entity.get("acquirer_data", {}).get("rrn")
                    or payment_entity.get("bank_transaction_id")
                    or ""
                ),
            )
        return {"ok": True, "event": event}

    if event == "payment.failed":
        order_id = payment_entity.get("order_id")
        if order_id:
            mark_order_failed(order_id, payload)
        return {"ok": True, "event": event}

    if event in ("refund.processed", "refund.failed"):
        refund_id = refund_entity.get("id")
        if refund_id:
            PaymentRefund.objects.filter(razorpay_refund_id=refund_id).update(
                status=RefundStatus.PROCESSED
                if event == "refund.processed"
                else RefundStatus.FAILED,
                raw_payload=payload,
            )
        return {"ok": True, "event": event}

    if event.startswith("settlement."):
        sid = settlement_entity.get("id")
        if sid:
            upsert_settlement_from_entity(settlement_entity)
        return {"ok": True, "event": event}

    logger.info("Unhandled Razorpay webhook event: %s", event)
    return {"ok": True, "event": event, "ignored": True}


@transaction.atomic
def create_refund(
    payment: Payment,
    *,
    amount: Decimal | None = None,
    reason: str = "",
    initiated_by=None,
) -> PaymentRefund:
    payment = Payment.objects.select_for_update().select_related("payment_order").get(pk=payment.pk)
    if payment.status == PaymentStatus.REFUNDED:
        existing = payment.refunds.order_by("-created_at").first()
        if existing:
            return existing
    refund_amount = Decimal(str(amount if amount is not None else payment.amount)).quantize(
        Decimal("0.01")
    )
    if refund_amount <= 0:
        raise RazorpayServiceError("Refund amount must be positive.")
    already = sum(
        (r.amount for r in payment.refunds.filter(status=RefundStatus.PROCESSED)),
        Decimal("0"),
    )
    if already + refund_amount > payment.amount + Decimal("0.01"):
        raise RazorpayServiceError("Refund exceeds captured amount.")

    client = get_razorpay_client()
    try:
        rp_refund = client.payment.refund(
            payment.razorpay_payment_id,
            {
                "amount": _amount_to_paise(refund_amount),
                "notes": {"reason": (reason or "")[:200]},
            },
        )
    except Exception as e:
        logger.exception("Razorpay refund failed for %s", payment.razorpay_payment_id)
        raise RazorpayServiceError(f"Refund failed: {e}") from e

    refund_id = rp_refund.get("id") if isinstance(rp_refund, dict) else None
    if not refund_id:
        raise RazorpayServiceError("Razorpay did not return a refund id.")

    status_val = RefundStatus.PROCESSED
    if isinstance(rp_refund, dict) and rp_refund.get("status") in ("pending", "created"):
        status_val = RefundStatus.PENDING

    refund = PaymentRefund.objects.create(
        payment=payment,
        razorpay_refund_id=refund_id,
        amount=refund_amount,
        status=status_val,
        reason=(reason or "")[:255],
        initiated_by=initiated_by,
        raw_payload=rp_refund if isinstance(rp_refund, dict) else {"raw": str(rp_refund)},
    )

    total_refunded = already + refund_amount
    if total_refunded >= payment.amount:
        payment.status = PaymentStatus.REFUNDED
    else:
        payment.status = PaymentStatus.PARTIALLY_REFUNDED
    payment.save(update_fields=["status", "updated_at"])
    return refund


def upsert_settlement_from_entity(entity: dict) -> PaymentSettlement:
    sid = str(entity.get("id") or "")
    if not sid:
        raise RazorpayServiceError("Missing settlement id")
    amount_paise = entity.get("amount") or 0
    amount = (Decimal(str(amount_paise)) / Decimal("100")).quantize(Decimal("0.01"))
    settled_on = None
    created_at = entity.get("created_at")
    if created_at:
        try:
            settled_on = timezone.datetime.fromtimestamp(
                int(created_at), tz=timezone.get_current_timezone()
            ).date()
        except Exception:
            settled_on = parse_date(str(created_at)[:10])

    obj, _ = PaymentSettlement.objects.update_or_create(
        settlement_id=sid,
        defaults={
            "bank_utr": str(entity.get("utr") or entity.get("bank_utr") or "")[:128],
            "amount": amount,
            "settled_on": settled_on,
            "raw_payload": entity,
        },
    )
    return obj


def sync_settlements(days: int = 7) -> int:
    """Pull recent settlements from Razorpay API. Returns count upserted."""
    client = get_razorpay_client()
    count = 0
    try:
        # Razorpay settlements.all supports count/skip
        result = client.settlement.all({"count": 100})
    except Exception:
        logger.exception("Failed to fetch Razorpay settlements")
        raise
    items = result.get("items") if isinstance(result, dict) else []
    for entity in items or []:
        try:
            upsert_settlement_from_entity(entity)
            count += 1
        except Exception:
            logger.exception("Failed to upsert settlement %s", entity.get("id"))
    return count


def get_successful_booking_payment(booking: Booking) -> Payment | None:
    order = (
        PaymentOrder.objects.filter(
            booking=booking,
            purpose=PaymentPurpose.BOOKING_SHORTFALL,
            status=PaymentOrderStatus.PAID,
        )
        .order_by("-created_at")
        .first()
    )
    if not order:
        return None
    return order.payments.filter(status__in=[PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED]).first()
