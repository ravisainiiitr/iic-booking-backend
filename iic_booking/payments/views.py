"""Razorpay payment API endpoints."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from decimal import InvalidOperation

from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.decorators import throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.models import Booking
from iic_booking.payments.fees import get_fee_percents
from iic_booking.payments.fees import set_fee_percents
from iic_booking.payments.models import Payment
from iic_booking.payments.models import PaymentIssue
from iic_booking.payments.models import PaymentIssueStatus
from iic_booking.payments.models import PaymentOrder
from iic_booking.payments.models import PaymentPurpose
from iic_booking.payments.models import RazorpayWebhookEvent
from iic_booking.payments.models import WebhookEventStatus
from iic_booking.payments.razorpay_service import RazorpayNotConfigured
from iic_booking.payments.razorpay_service import RazorpayServiceError
from iic_booking.payments.razorpay_service import confirm_gateway_payment
from iic_booking.payments.razorpay_service import create_order
from iic_booking.payments.razorpay_service import create_refund
from iic_booking.payments.razorpay_service import handle_webhook
from iic_booking.payments.razorpay_service import settle_order_success
from iic_booking.payments.razorpay_service import verify_checkout_signature
from iic_booking.payments.throttles import PaymentUserThrottle
from iic_booking.payments.throttles import PaymentWebhookThrottle
from iic_booking.users.models import UserType
from iic_booking.users.repositories.wallet_repository import WalletRepository
from iic_booking.users.repositories.wallet_repository import (
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
    from iic_booking.payments.gateway_mode import public_status

    fee_percent, fee_gst_percent = get_fee_percents()
    return Response(
        {
            "fee_percent": str(fee_percent),
            "fee_gst_percent": str(fee_gst_percent),
            "gateway": public_status(request.user),
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
@throttle_classes([PaymentUserThrottle])
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
        from iic_booking.users.api.wallet_views import (
            online_gateway_recharge_disabled_response,
        )
        from iic_booking.users.models.wallet_sric_settings import (
            online_gateway_recharge_enabled,
        )
        from iic_booking.users.student_wallet_recharge import (
            assert_iitr_student_may_recharge,
        )

        if not online_gateway_recharge_enabled(department=request.data.get("department_id")):
            return online_gateway_recharge_disabled_response()

        try:
            amount = Decimal(str(request.data.get("amount") or "0")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)

        department_id = request.data.get("department_id")
        if not department_id:
            return Response({"error": "department_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        forbidden = assert_iitr_student_may_recharge(
            request.user, department_id=int(department_id)
        )
        if forbidden:
            return Response({"error": forbidden}, status=status.HTTP_403_FORBIDDEN)

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
        return Response({"error": str(e), "code": "gateway_unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except RazorpayServiceError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception("create-order failed")
        return Response({"error": "The payment could not be started. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([PaymentUserThrottle])
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
        entity = {}
        if order.status != "PAID":
            # The signature only proves the browser saw a payment: Razorpay must confirm it is captured for the
            # order amount before anything is delivered.
            try:
                entity = confirm_gateway_payment(order, payment_id)
            except RazorpayServiceError as e:
                return Response(
                    {"error": f"{e} If money was deducted, the payment will be updated automatically once Razorpay "
                              "confirms it.", "code": "payment_not_confirmed"},
                    status=status.HTTP_409_CONFLICT,
                )
        try:
            order, payment = settle_order_success(
                razorpay_order_id=order_id,
                razorpay_payment_id=payment_id,
                razorpay_signature=signature,
                verified_via="CHECKOUT",
                raw_payload={"source": "checkout", "razorpay_order_id": order_id, "razorpay_payment_id": payment_id},
                method=str(entity.get("method") or ""),
                gateway_reference=str((entity.get("acquirer_data") or {}).get("rrn") or entity.get("bank_transaction_id") or "")[:128],
            )
        except RazorpayServiceError as e:
            if entity:  # Razorpay confirmed the capture, so the money is taken: hand it to Finance
                from iic_booking.payments.issues import raise_issue
                from iic_booking.payments.models import PaymentIssueKind

                raise_issue(PaymentIssueKind.NOT_APPLIED, key=payment_id, order=order, razorpay_payment_id=payment_id,
                            amount=order.total_amount, detail=f"Captured at checkout but not applied: {e}")
            raise
        from iic_booking.payments.reconciliation import record_gateway_charges

        record_gateway_charges(payment_id, entity)
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


def _event_summary(payload: dict) -> dict:
    inner = (payload or {}).get("payload") or {}
    payment = ((inner.get("payment") or {}).get("entity")) or {}
    refund = ((inner.get("refund") or {}).get("entity")) or {}
    settlement = ((inner.get("settlement") or {}).get("entity")) or {}
    entity = payment or refund or settlement
    try:
        amount = int(entity.get("amount")) if entity.get("amount") is not None else None
    except (TypeError, ValueError):
        amount = None
    return {
        "event_type": str((payload or {}).get("event") or "")[:64],
        "razorpay_order_id": str(payment.get("order_id") or "")[:64],
        "razorpay_payment_id": str(payment.get("id") or refund.get("payment_id") or "")[:64],
        "razorpay_refund_id": str(refund.get("id") or "")[:64],
        "razorpay_settlement_id": str(settlement.get("id") or "")[:64],
        "amount_paise": amount,
        "currency": str(entity.get("currency") or "")[:8],
        "entity_status": str(entity.get("status") or "")[:32],
    }


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PaymentWebhookThrottle])
def razorpay_webhook(request):
    """Signed Razorpay events. The signature is checked on the raw body first; each event ID is processed once.

    A signed event that cannot be applied is answered 200 and raised as a payment issue (a 4xx answer makes Razorpay
    retry for 24 hours and then disable the webhook). Only an unexpected error answers 500, so Razorpay retries it.
    """
    import hashlib

    from django.db import transaction
    from django.utils import timezone

    from iic_booking.payments.razorpay_service import verify_webhook_signature

    signature = request.headers.get("X-Razorpay-Signature") or request.META.get("HTTP_X_RAZORPAY_SIGNATURE") or ""
    body = request.body
    if not verify_webhook_signature(body, signature):
        return Response({"error": "Invalid webhook signature."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except Exception:
        return Response({"error": "Invalid JSON."}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(payload, dict):
        return Response({"error": "Invalid JSON."}, status=status.HTTP_400_BAD_REQUEST)

    event_id = (request.headers.get("X-Razorpay-Event-Id") or "").strip()[:64]
    if not event_id:  # Razorpay always sends one; fall back to the body's hash so a resend is still recognised
        event_id = "body-" + hashlib.sha256(body if isinstance(body, bytes) else str(body).encode()).hexdigest()[:59]
    with transaction.atomic():
        event, created = RazorpayWebhookEvent.objects.select_for_update().get_or_create(
            event_id=event_id, defaults=_event_summary(payload)
        )
        if not created:
            event.attempts += 1
            if event.status in (WebhookEventStatus.PROCESSED, WebhookEventStatus.IGNORED):
                event.save(update_fields=["attempts"])
                return Response({"ok": True, "event": event.event_type, "duplicate": True})
            event.status = WebhookEventStatus.PROCESSING
            event.save(update_fields=["attempts", "status"])
    try:
        with transaction.atomic():
            result = handle_webhook(payload, body, signature)
    except Exception:
        logger.exception("webhook handling failed for event %s", event_id)
        RazorpayWebhookEvent.objects.filter(pk=event.pk).update(
            status=WebhookEventStatus.FAILED, result="Unexpected error; Razorpay will retry.", processed_at=timezone.now()
        )
        return Response({"error": "Webhook processing failed."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    ignored = result.get("ignored")
    RazorpayWebhookEvent.objects.filter(pk=event.pk).update(
        status=WebhookEventStatus.IGNORED if ignored else WebhookEventStatus.PROCESSED,
        result=(str(ignored) if ignored and ignored is not True else ("not handled" if ignored else "applied"))[:255],
        processed_at=timezone.now(),
    )
    return Response(result)


def _issue(i: PaymentIssue) -> dict:
    return {
        "id": i.pk, "kind": i.kind, "kind_label": i.get_kind_display(), "status": i.status,
        "razorpay_order_id": i.razorpay_order_id, "razorpay_payment_id": i.razorpay_payment_id,
        "amount": str(i.amount) if i.amount is not None else None, "mode": i.mode, "detail": i.detail,
        "raised_at": i.raised_at.isoformat(), "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
        "resolution_note": i.resolution_note,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payment_issues(request):
    """Finance and Main Admin: payments that reached Razorpay but were not applied normally."""
    if not _is_finance_or_admin(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    state = request.query_params.get("status", PaymentIssueStatus.OPEN)
    queryset = PaymentIssue.objects.all()
    if state in PaymentIssueStatus.values:
        queryset = queryset.filter(status=state)
    return Response({"results": [_issue(i) for i in queryset[:500]]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def payment_issue_resolve(request, issue_id: int):
    """Close an issue with a note once the money has been refunded or applied by hand. Nothing else changes."""
    from django.utils import timezone

    if not _is_finance_or_admin(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    note = str(request.data.get("note") or "").strip()
    if len(note) < 5:
        return Response({"error": "Give a note of at least 5 characters."}, status=status.HTTP_400_BAD_REQUEST)
    issue = PaymentIssue.objects.filter(pk=issue_id, status=PaymentIssueStatus.OPEN).first()
    if issue is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    issue.status, issue.resolved_at, issue.resolved_by, issue.resolution_note = (
        PaymentIssueStatus.RESOLVED, timezone.now(), request.user, note[:2000]
    )
    issue.save(update_fields=["status", "resolved_at", "resolved_by", "resolution_note"])
    return Response(_issue(issue))


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
    """Main Admin / Finance refund of a captured Razorpay payment.

    Operators and OICs refund through the booking refund flow (``refund_booking_internal``), which ties the
    gateway refund to a specific booking; this endpoint refunds an arbitrary payment and is finance-only.
    """
    if not _is_finance_or_admin(request.user):
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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def reconciliation_summary(request):
    """Finance and Main Admin: collected, refunded, gateway charges and settled money for a date range (last 30 days
    by default), with open issues. ``mode=test`` shows test-mode orders alone; otherwise they are excluded.
    ``export=csv`` downloads the issue list."""
    import csv
    from datetime import timedelta

    from django.http import HttpResponse
    from django.utils import timezone
    from django.utils.dateparse import parse_date

    from iic_booking.payments.reconciliation import summary

    if not _is_finance_or_admin(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    today = timezone.localdate()
    date_to = parse_date(request.query_params.get("date_to") or "") or today
    date_from = parse_date(request.query_params.get("date_from") or "") or (date_to - timedelta(days=30))
    if request.query_params.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="payment-issues.csv"'
        writer = csv.writer(response)
        writer.writerow(["Raised", "Kind", "Status", "Order", "Payment", "Amount", "Mode", "Detail", "Resolution"])
        issues = PaymentIssue.objects.filter(raised_at__date__gte=date_from, raised_at__date__lte=date_to).order_by("raised_at")
        for i in issues.iterator():
            writer.writerow([_csv_safe(v) for v in (i.raised_at.isoformat(), i.get_kind_display(), i.status, i.razorpay_order_id,
                                                    i.razorpay_payment_id, i.amount, i.mode, i.detail, i.resolution_note)])
        return response
    return Response(summary(date_from, date_to, mode="test" if request.query_params.get("mode") == "test" else None))


def _csv_safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payment_receipt(request, razorpay_payment_id: str):
    """Receipt for a captured online payment: the payer, Finance or Main Admin. Others get 404, so a payment ID
    cannot be probed."""
    from iic_booking.payments.receipts import receipt_allowed
    from iic_booking.payments.receipts import receipt_for

    payment = (
        Payment.objects.select_related("payment_order__user", "payment_order__booking__equipment", "payment_order__department")
        .filter(razorpay_payment_id=razorpay_payment_id)
        .first()
    )
    if payment is None or not (payment.payment_order.user_id == request.user.id or _is_finance_or_admin(request.user)):
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if not receipt_allowed(payment):
        return Response({"error": "No receipt: this payment is not confirmed.", "code": "not_confirmed"}, status=status.HTTP_409_CONFLICT)
    return Response(receipt_for(payment))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_payment_preview(request, booking_id: int):
    """What the booking owner will pay online, calculated on the server exactly as the order will be."""
    from iic_booking.external_services.checkout import equipment_image_url
    from iic_booking.payments.purpose import booking_purpose, profile_type_of
    from iic_booking.payments.fees import compute_fee_breakup
    from iic_booking.payments.gateway_mode import public_status

    booking = Booking.objects.select_related("equipment", "settlement_department").filter(pk=booking_id).first()
    if booking is None or booking.user_id != request.user.id:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    due = Decimal(str(booking.amount_due or 0))
    breakup = compute_fee_breakup(due)
    return Response({
        "booking_reference": booking.virtual_booking_id or str(booking.booking_id),
        "equipment": getattr(booking.equipment, "name", ""),
        "equipment_image_url": equipment_image_url(booking.equipment),
        "payment_purpose": booking_purpose(getattr(booking.equipment, "name", ""), profile_type_of(booking, booking.equipment)),
        "department": getattr(booking.settlement_department, "name", ""),
        "status": booking.get_status_display(),
        "total_charge": str(booking.total_charge or 0),
        "wallet_applied": str(getattr(booking, "wallet_amount_applied", 0) or 0),
        "amount_due": str(due),
        "breakup": {
            "base_amount": str(breakup.base_amount), "convenience_fee": str(breakup.convenience_fee),
            "fee_gst": str(breakup.fee_gst), "total_amount": str(breakup.total_amount),
            "fee_percent": str(breakup.fee_percent), "fee_gst_percent": str(breakup.fee_gst_percent),
        },
        "gateway": public_status(request.user),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_payment_receipts(request):
    """The signed-in user's confirmed online payments (bookings and wallet recharges), for downloading receipts later."""
    from iic_booking.payments.receipts import receipts_for

    return Response({"results": receipts_for(Payment.objects.filter(payment_order__user=request.user))})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_payment_receipts(request, booking_id: int):
    """Online payments for one booking: its balance payment, or the external checkout that created it. The booking's
    owner, Finance or Main Admin; others get 404."""
    from django.db.models import Q

    from iic_booking.payments.receipts import receipts_for

    booking = Booking.objects.filter(pk=booking_id).only("pk", "user_id").first()
    if booking is None or not (booking.user_id == request.user.id or _is_finance_or_admin(request.user)):
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    payments = Payment.objects.filter(Q(payment_order__booking_id=booking.pk) | Q(payment_order__external_checkout__booking_id=booking.pk))
    return Response({"results": receipts_for(payments)})
