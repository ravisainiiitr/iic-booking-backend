"""Razorpay payment API endpoints."""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.models import Booking
from iic_booking.payments.fees import get_fee_percents, set_fee_percents
from iic_booking.payments.models import Payment, PaymentOrder, PaymentPurpose
from iic_booking.payments.razorpay_service import (
    RazorpayNotConfigured,
    RazorpayServiceError,
    create_order,
    create_refund,
    settle_order_success,
    handle_webhook,
    verify_checkout_signature,
)
from iic_booking.users.models import UserType
from iic_booking.users.repositories.wallet_repository import (
    WalletRepository,
    resolve_internal_department_for_wallet_recharge,
)

logger = logging.getLogger(__name__)


def _is_finance_or_admin(user) -> bool:
    ut = getattr(user, "user_type", None)
    return ut in (UserType.ADMIN, UserType.FINANCE)


def _is_main_admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.ADMIN


def _serialize_order(order: PaymentOrder) -> dict:
    return {
        "id": order.id,
        "razorpay_order_id": order.razorpay_order_id,
        "purpose": order.purpose,
        "status": order.status,
        "base_amount": str(order.base_amount),
        "convenience_fee": str(order.convenience_fee),
        "fee_gst": str(order.fee_gst),
        "total_amount": str(order.total_amount),
        "currency": order.currency,
        "booking_id": order.booking_id,
        "wallet_id": order.wallet_id,
        "department_id": order.department_id,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def fee_settings(request):
    fee_percent, fee_gst_percent = get_fee_percents()
    return Response(
        {
            "fee_percent": str(fee_percent),
            "fee_gst_percent": str(fee_gst_percent),
            "RAZORPAY_CONVENIENCE_FEE_PERCENT": str(fee_percent),
            "RAZORPAY_CONVENIENCE_FEE_GST_PERCENT": str(fee_gst_percent),
        }
    )


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def admin_fee_settings(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Only Main Admin can update fee settings."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        fee_percent, fee_gst_percent = get_fee_percents()
        return Response(
            {
                "fee_percent": str(fee_percent),
                "fee_gst_percent": str(fee_gst_percent),
            }
        )
    try:
        fee_percent = Decimal(str(request.data.get("fee_percent", request.data.get("RAZORPAY_CONVENIENCE_FEE_PERCENT", "0"))))
        fee_gst_percent = Decimal(
            str(request.data.get("fee_gst_percent", request.data.get("RAZORPAY_CONVENIENCE_FEE_GST_PERCENT", "18")))
        )
    except (InvalidOperation, TypeError):
        return Response({"error": "Invalid fee values."}, status=status.HTTP_400_BAD_REQUEST)
    if fee_percent < 0 or fee_gst_percent < 0:
        return Response({"error": "Fee percents cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)
    set_fee_percents(fee_percent, fee_gst_percent)
    fee_percent, fee_gst_percent = get_fee_percents()
    return Response(
        {
            "fee_percent": str(fee_percent),
            "fee_gst_percent": str(fee_gst_percent),
            "message": "Fee settings updated.",
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def razorpay_create_order(request):
    purpose = (request.data.get("purpose") or "").strip().upper()
    if purpose not in PaymentPurpose.values:
        return Response({"error": "Invalid purpose."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        if purpose == PaymentPurpose.BOOKING_SHORTFALL:
            booking_id = request.data.get("booking_id")
            if not booking_id:
                return Response({"error": "booking_id required."}, status=status.HTTP_400_BAD_REQUEST)
            booking = get_object_or_404(Booking, pk=int(booking_id))
            order, payload = create_order(
                user=request.user,
                purpose=purpose,
                booking=booking,
                department=booking.settlement_department,
            )
            return Response(payload, status=status.HTTP_201_CREATED)

        # WALLET_RECHARGE
        from iic_booking.users.student_wallet_recharge import assert_iitr_student_may_recharge

        forbidden = assert_iitr_student_may_recharge(request.user)
        if forbidden:
            return Response({"error": forbidden}, status=status.HTTP_403_FORBIDDEN)

        try:
            amount = Decimal(str(request.data.get("amount") or "0")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)

        department_id = request.data.get("department_id")
        if not department_id:
            return Response({"error": "department_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        wallet = request.user.get_accessible_wallet()
        if not wallet:
            if request.user.can_have_wallet():
                wallet, _ = WalletRepository.get_or_create(request.user)
            if not wallet:
                return Response({"error": "No wallet access."}, status=status.HTTP_403_FORBIDDEN)

        department = resolve_internal_department_for_wallet_recharge(wallet, int(department_id))
        if not department:
            return Response({"error": "Invalid department."}, status=status.HTTP_400_BAD_REQUEST)

        order, payload = create_order(
            user=request.user,
            purpose=purpose,
            base_amount=amount,
            wallet=wallet,
            department=department,
        )
        return Response(payload, status=status.HTTP_201_CREATED)

    except RazorpayNotConfigured as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except RazorpayServiceError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.exception("create-order failed")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def razorpay_verify(request):
    order_id = request.data.get("razorpay_order_id") or request.data.get("order_id")
    payment_id = request.data.get("razorpay_payment_id") or request.data.get("payment_id")
    signature = request.data.get("razorpay_signature") or request.data.get("signature")
    if not all([order_id, payment_id, signature]):
        return Response(
            {"error": "razorpay_order_id, razorpay_payment_id, and razorpay_signature are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    order = PaymentOrder.objects.filter(razorpay_order_id=order_id).first()
    if not order:
        return Response({"error": "Order not found."}, status=status.HTTP_404_NOT_FOUND)
    if order.user_id != request.user.id and not _is_finance_or_admin(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

    try:
        if not verify_checkout_signature(order_id, payment_id, signature):
            return Response({"error": "Invalid payment signature."}, status=status.HTTP_400_BAD_REQUEST)
        order, payment = settle_order_success(
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=signature,
            verified_via="CHECKOUT",
            raw_payload=dict(request.data),
        )
    except RazorpayNotConfigured as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except RazorpayServiceError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    booking_payload = None
    if order.booking_id:
        booking = order.booking
        booking.refresh_from_db()
        booking_payload = {
            "booking_id": booking.booking_id,
            "status": booking.status,
            "amount_due": str(booking.amount_due),
            "payment_settled_at": booking.payment_settled_at.isoformat() if booking.payment_settled_at else None,
        }

    wallet_balance = None
    if order.wallet_id:
        order.wallet.refresh_from_db()
        wallet_balance = str(order.wallet.total_balance)

    return Response(
        {
            "message": "Payment verified successfully.",
            "order": _serialize_order(order),
            "payment_id": payment.razorpay_payment_id,
            "booking": booking_payload,
            "wallet_balance": wallet_balance,
        }
    )


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def razorpay_webhook(request):
    signature = request.headers.get("X-Razorpay-Signature") or request.META.get("HTTP_X_RAZORPAY_SIGNATURE") or ""
    body = request.body
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except Exception:
        return Response({"error": "Invalid JSON."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        result = handle_webhook(payload, body, signature)
    except RazorpayServiceError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception("webhook handling failed")
        return Response({"error": "Webhook processing failed."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def razorpay_order_detail(request, order_id: int):
    order = get_object_or_404(PaymentOrder, pk=order_id)
    if order.user_id != request.user.id and not _is_finance_or_admin(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    payments = [
        {
            "razorpay_payment_id": p.razorpay_payment_id,
            "amount": str(p.amount),
            "status": p.status,
            "method": p.method,
            "verified_via": p.verified_via,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in order.payments.all()[:20]
    ]
    data = _serialize_order(order)
    data["payments"] = payments
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def razorpay_refund(request):
    """Admin/finance/operator refund of a captured Razorpay payment."""
    from iic_booking.equipment.api_views import check_operator_permission

    if not (_is_finance_or_admin(request.user) or check_operator_permission(request.user)):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

    payment_id = request.data.get("razorpay_payment_id") or request.data.get("payment_id")
    payment_pk = request.data.get("payment_pk")
    if payment_pk:
        payment = get_object_or_404(Payment, pk=int(payment_pk))
    elif payment_id:
        payment = get_object_or_404(Payment, razorpay_payment_id=payment_id)
    else:
        return Response({"error": "payment_id required."}, status=status.HTTP_400_BAD_REQUEST)

    amount = None
    if request.data.get("amount") is not None:
        try:
            amount = Decimal(str(request.data.get("amount"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)

    reason = (request.data.get("reason") or "")[:255]
    try:
        refund = create_refund(
            payment,
            amount=amount,
            reason=reason,
            initiated_by=request.user,
        )
    except RazorpayNotConfigured as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except RazorpayServiceError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {
            "message": "Refund initiated.",
            "razorpay_refund_id": refund.razorpay_refund_id,
            "amount": str(refund.amount),
            "status": refund.status,
        }
    )
