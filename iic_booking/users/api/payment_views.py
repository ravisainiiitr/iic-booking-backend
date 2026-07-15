"""SBIePay, UTR receipts, and finance payment APIs."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.booking_payment_service import booking_payment_fully_settled
from iic_booking.equipment.models import Booking, BookingStatus, IstemFbrStatus, charge_profile_requires_istem_fbr, initial_istem_fbr_fields_for_charge_profile
from iic_booking.users.models import User, UserType, Wallet
from iic_booking.users.models.payment import (
    DepartmentPaymentReceipt,
    DepartmentPaymentReceiptPurpose,
    DepartmentPaymentReceiptStatus,
    PaymentGateway,
    PaymentGatewayStatus,
    PaymentGatewayTransaction,
    PaymentPurpose,
)
from iic_booking.users.models.wallet import WalletRechargeRequest
from iic_booking.users.payment_settlement import settle_payment_gateway_transaction
from iic_booking.users.repositories.wallet_repository import WalletRepository, resolve_internal_department_for_wallet_recharge
from iic_booking.users.sbiepay_service import (
    build_initiate_payload,
    generate_merchant_order_ref,
    parse_gateway_response,
)


def _is_finance_or_admin(user) -> bool:
    ut = getattr(user, "user_type", None)
    return ut in (UserType.ADMIN, UserType.FINANCE)


def _serialize_receipt(r: DepartmentPaymentReceipt) -> dict:
    dept = r.department
    receipt_file_url = None
    if getattr(r, "receipt_file", None):
        try:
            if r.receipt_file:
                receipt_file_url = r.receipt_file.url
        except Exception:
            receipt_file_url = None
    return {
        "id": r.id,
        "utr_reference": r.utr_reference,
        "amount": str(r.amount),
        "purpose": r.purpose,
        "status": r.status,
        "department_id": dept.id if dept else None,
        "department_name": dept.name if dept else "",
        "department_code": dept.code if dept else "",
        "user_email": r.user.email if r.user_id else "",
        "booking_id": r.booking_id,
        "wallet_recharge_request_id": r.wallet_recharge_request_id,
        "payment_date": r.payment_date.isoformat() if r.payment_date else None,
        "finance_processed_at": r.finance_processed_at.isoformat() if r.finance_processed_at else None,
        "finance_remarks": r.finance_remarks,
        "receipt_file_url": receipt_file_url,
        "has_receipt_file": bool(receipt_file_url),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sbiepay_initiate(request):
    """
    Start SBIePay payment for wallet recharge or booking shortfall.
    Body: purpose, amount, department_id; optional booking_id for BOOKING_SHORTFALL.
    """
    purpose = (request.data.get("purpose") or "").strip().upper()
    if purpose not in PaymentPurpose.values:
        return Response({"error": "Invalid purpose."}, status=status.HTTP_400_BAD_REQUEST)

    if purpose == PaymentPurpose.WALLET_RECHARGE:
        from iic_booking.users.student_wallet_recharge import assert_iitr_student_may_recharge

        forbidden = assert_iitr_student_may_recharge(request.user)
        if forbidden:
            return Response({"error": forbidden}, status=status.HTTP_403_FORBIDDEN)

    try:
        amount = Decimal(str(request.data.get("amount") or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)
    if amount <= 0:
        return Response({"error": "Amount must be positive."}, status=status.HTTP_400_BAD_REQUEST)

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

    booking = None
    if purpose == PaymentPurpose.BOOKING_SHORTFALL:
        booking_id = request.data.get("booking_id")
        if not booking_id:
            return Response({"error": "booking_id required."}, status=status.HTTP_400_BAD_REQUEST)
        booking = get_object_or_404(Booking, pk=int(booking_id))
        if booking.user_id != request.user.id:
            return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        due = Decimal(str(booking.amount_due or 0))
        if due <= 0:
            return Response({"error": "No balance due on this booking."}, status=status.HTTP_400_BAD_REQUEST)
        if amount != due:
            return Response(
                {"error": f"Amount must match balance due (₹{due:.2f})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    ref = generate_merchant_order_ref("IICW" if purpose == PaymentPurpose.WALLET_RECHARGE else "IICB")
    try:
        payload = build_initiate_payload(amount_inr=amount, merchant_order_ref=ref)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    PaymentGatewayTransaction.objects.create(
        gateway=PaymentGateway.SBIEPAY,
        merchant_order_ref=ref,
        amount=amount,
        purpose=purpose,
        user=request.user,
        wallet=wallet,
        department=department,
        booking=booking,
        status=PaymentGatewayStatus.PENDING,
    )
    return Response(payload, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes([])
def sbiepay_return_success(request):
    """SBIePay success return URL — verify and settle."""
    enc = request.POST.get("encData") or request.GET.get("encData") or request.data.get("encData")
    ref = request.POST.get("merchant_order_ref") or request.GET.get("merchantOrderRef")
    return _handle_sbiepay_return(request, enc, ref, success=True)


@api_view(["GET", "POST"])
@permission_classes([])
def sbiepay_return_failure(request):
    enc = request.POST.get("encData") or request.GET.get("encData")
    ref = request.POST.get("merchant_order_ref") or request.GET.get("merchantOrderRef")
    return _handle_sbiepay_return(request, enc, ref, success=False)


def _handle_sbiepay_return(request, enc, ref, *, success: bool):
    parsed = parse_gateway_response(enc or "") if enc else {}
    if not ref and parsed:
        ref = parsed.get("merchant_order_ref") or parsed.get("f4")
    if not ref:
        return Response({"error": "Missing order reference."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        txn = PaymentGatewayTransaction.objects.get(merchant_order_ref=ref)
    except PaymentGatewayTransaction.DoesNotExist:
        return Response({"error": "Unknown transaction."}, status=status.HTTP_404_NOT_FOUND)

    txn.raw_response = {**(txn.raw_response or {}), "return": parsed, "success_flag": success}
    if parsed.get("gateway_ref"):
        txn.gateway_transaction_id = str(parsed["gateway_ref"])[:128]
    if not success:
        txn.status = PaymentGatewayStatus.FAILED
        txn.save(update_fields=["status", "raw_response", "gateway_transaction_id", "updated_at"])
        frontend = getattr(settings, "FRONTEND_URL", "http://localhost:5173")
        if txn.booking_id:
            return Response(
                {"redirect": f"{frontend}/bookings/{txn.booking_id}/payment?status=failed&ref={ref}"},
                status=status.HTTP_200_OK,
            )
        return Response(
            {"redirect": f"{frontend}/wallet?payment=failed&ref={ref}"},
            status=status.HTTP_200_OK,
        )

    try:
        settle_payment_gateway_transaction(txn)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    frontend = getattr(settings, "FRONTEND_URL", "http://localhost:5173")
    if txn.booking_id:
        return Response(
            {"redirect": f"{frontend}/bookings/{txn.booking_id}/next-steps?payment=success&ref={ref}"},
            status=status.HTTP_200_OK,
        )
    return Response(
        {"redirect": f"{frontend}/wallet?payment=success&ref={ref}"},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sbiepay_transaction_status(request):
    ref = (request.query_params.get("ref") or "").strip()
    if not ref:
        return Response({"error": "ref is required."}, status=status.HTTP_400_BAD_REQUEST)
    txn = get_object_or_404(PaymentGatewayTransaction, merchant_order_ref=ref, user=request.user)
    return Response(
        {
            "merchant_order_ref": txn.merchant_order_ref,
            "status": txn.status,
            "amount": str(txn.amount),
            "purpose": txn.purpose,
            "booking_id": txn.booking_id,
            "verified_at": txn.verified_at.isoformat() if txn.verified_at else None,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_payment_utr(request):
    """
    Offline UTR for wallet recharge or booking shortfall (govt / bank deposit).
    Body: utr_reference, amount, department_id, purpose; booking_id or recharge_request_id.
    """
    from iic_booking.users.student_wallet_recharge import (
        is_iitr_student,
        student_otp_offline_forbidden_message,
    )

    purpose = (request.data.get("purpose") or "").strip().upper()
    # IITR Students must use the receipt-file endpoint for wallet recharge offline.
    if is_iitr_student(request.user) and purpose == DepartmentPaymentReceiptPurpose.WALLET_RECHARGE:
        return Response(
            {"error": student_otp_offline_forbidden_message()},
            status=status.HTTP_403_FORBIDDEN,
        )

    utr = (request.data.get("utr_reference") or "").strip()
    if not utr:
        return Response({"error": "UTR is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        amount = Decimal(str(request.data.get("amount") or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)
    if purpose not in DepartmentPaymentReceiptPurpose.values:
        return Response({"error": "Invalid purpose."}, status=status.HTTP_400_BAD_REQUEST)

    department_id = request.data.get("department_id")
    if not department_id:
        return Response({"error": "department_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    wallet = request.user.get_accessible_wallet()
    if not wallet:
        return Response({"error": "No wallet."}, status=status.HTTP_403_FORBIDDEN)
    department = resolve_internal_department_for_wallet_recharge(wallet, int(department_id))
    if not department:
        return Response({"error": "Invalid department."}, status=status.HTTP_400_BAD_REQUEST)

    booking = None
    recharge_req = None
    if purpose == DepartmentPaymentReceiptPurpose.BOOKING_SHORTFALL:
        booking = get_object_or_404(Booking, pk=int(request.data.get("booking_id")))
        if booking.user_id != request.user.id:
            return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        due = Decimal(str(booking.amount_due or 0))
        if amount != due:
            return Response({"error": f"Amount must be ₹{due:.2f}."}, status=status.HTTP_400_BAD_REQUEST)
    else:
        rid = request.data.get("recharge_request_id")
        if rid:
            recharge_req = get_object_or_404(WalletRechargeRequest, pk=int(rid), user=request.user)
            recharge_req.utr_reference = utr[:255]
            recharge_req.save(update_fields=["utr_reference", "updated_at"])

    payment_date = request.data.get("payment_date")
    pd = None
    if payment_date:
        from datetime import datetime

        try:
            pd = datetime.strptime(str(payment_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            pass

    receipt, created = DepartmentPaymentReceipt.objects.get_or_create(
        utr_reference=utr[:64],
        department=department,
        defaults={
            "user": request.user,
            "amount": amount,
            "purpose": purpose,
            "booking": booking,
            "wallet_recharge_request": recharge_req,
            "payment_date": pd,
        },
    )
    if not created and receipt.user_id != request.user.id:
        return Response({"error": "UTR already registered."}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {"message": "UTR submitted for finance verification.", "receipt": _serialize_receipt(receipt)},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_wallet_recharge_receipt(request):
    """
    IITR Student offline wallet recharge: multipart amount, department_id, receipt_file;
    optional utr_reference. Credits faculty accessible wallet when finance processes.
    """
    import uuid

    from iic_booking.users.student_wallet_recharge import (
        assert_iitr_student_may_recharge,
        is_iitr_student,
    )

    # Individual students / others may also use this path when they have wallet access.
    # IITR Students are gated by the admin flag.
    forbidden = assert_iitr_student_may_recharge(request.user)
    if forbidden:
        return Response({"error": forbidden}, status=status.HTTP_403_FORBIDDEN)

    upload = request.FILES.get("receipt_file") or request.FILES.get("file")
    if not upload:
        return Response(
            {"error": "Payment receipt file is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        amount = Decimal(str(request.data.get("amount") or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)
    if amount <= 0:
        return Response({"error": "Amount must be positive."}, status=status.HTTP_400_BAD_REQUEST)

    department_id = request.data.get("department_id")
    if not department_id:
        return Response({"error": "department_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    wallet = request.user.get_accessible_wallet()
    if not wallet:
        if request.user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(request.user)
        if not wallet:
            return Response(
                {
                    "error": (
                        "No wallet access. IITR Students must be linked to a faculty wallet "
                        "before recharging."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

    department = resolve_internal_department_for_wallet_recharge(wallet, int(department_id))
    if not department:
        return Response({"error": "Invalid department."}, status=status.HTTP_400_BAD_REQUEST)

    utr = (request.data.get("utr_reference") or "").strip()
    if not utr:
        utr = f"FILE-{request.user.id}-{uuid.uuid4().hex[:12]}"
    utr = utr[:64]

    payment_date = request.data.get("payment_date")
    pd = None
    if payment_date:
        from datetime import datetime

        try:
            pd = datetime.strptime(str(payment_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            pass

    if DepartmentPaymentReceipt.objects.filter(utr_reference=utr, department=department).exists():
        return Response(
            {"error": "This UTR / reference is already registered for the department."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    receipt = DepartmentPaymentReceipt.objects.create(
        utr_reference=utr,
        department=department,
        user=request.user,
        amount=amount,
        purpose=DepartmentPaymentReceiptPurpose.WALLET_RECHARGE,
        payment_date=pd,
        receipt_file=upload,
    )

    msg = (
        "Payment receipt submitted for finance verification. "
        "Funds will be parked in the faculty wallet after approval."
        if is_iitr_student(request.user)
        else "Payment receipt submitted for finance verification."
    )
    return Response(
        {"message": msg, "receipt": _serialize_receipt(receipt)},
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def finance_payment_receipts_list(request):
    if not _is_finance_or_admin(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    qs = DepartmentPaymentReceipt.objects.select_related("department", "user", "booking").order_by("-created_at")
    st = (request.query_params.get("status") or "").strip().upper()
    if st:
        qs = qs.filter(status=st)
    dept = request.query_params.get("department_id")
    if dept:
        qs = qs.filter(department_id=int(dept))
    return Response({"receipts": [_serialize_receipt(r) for r in qs[:500]], "count": qs.count()})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def finance_payment_receipt_process(request, receipt_id: int):
    if not _is_finance_or_admin(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    receipt = get_object_or_404(
        DepartmentPaymentReceipt.objects.select_related("booking", "wallet_recharge_request", "department", "user"),
        pk=receipt_id,
    )
    if receipt.status != DepartmentPaymentReceiptStatus.PENDING:
        return Response({"error": "Already processed."}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        receipt.status = DepartmentPaymentReceiptStatus.PROCESSED
        receipt.finance_processed_at = timezone.now()
        receipt.finance_processed_by = request.user
        receipt.finance_remarks = (request.data.get("remarks") or "")[:2000]
        receipt.save()

        if receipt.purpose == DepartmentPaymentReceiptPurpose.BOOKING_SHORTFALL and receipt.booking_id:
            booking = Booking.objects.select_for_update().get(pk=receipt.booking_id)
            booking.payment_settled_at = timezone.now()
            if booking.status == BookingStatus.PENDING_PAYMENT:
                booking.status = BookingStatus.BOOKED
            if not booking.istem_fbr_status and charge_profile_requires_istem_fbr(booking.charge_profile):
                for k, v in initial_istem_fbr_fields_for_charge_profile(booking.charge_profile).items():
                    setattr(booking, k, v)
            booking.save(update_fields=["payment_settled_at", "status", "istem_fbr_status", "updated_at"])

        elif receipt.purpose == DepartmentPaymentReceiptPurpose.WALLET_RECHARGE:
            wallet = receipt.user.get_accessible_wallet()
            if wallet:
                from iic_booking.users.repositories.wallet_repository import SubWalletRepository

                sub = SubWalletRepository.get_or_create(wallet, receipt.department)
                sub.credit(
                    receipt.amount,
                    f"Offline UTR {receipt.utr_reference} — {receipt.department.name}",
                    related_user=receipt.user,
                )

    return Response({"message": "Processed.", "receipt": _serialize_receipt(receipt)})
