"""API views for faculty wallet-to-wallet peer transfers."""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models import Department, DepartmentType, UserType, WalletPeerTransfer, WalletPeerTransferStatus
from iic_booking.users.models.wallet import SubWallet
from iic_booking.users.wallet_peer_transfer import (
    PeerTransferError,
    confirm_transfer_with_otp,
    create_pending_transfer,
    list_eligible_recipients,
    notify_peer_transfer_completed,
    send_transfer_otp_email,
)
from iic_booking.users.wallet_recharge_ops import resolve_department_grant_code

logger = logging.getLogger(__name__)
User = get_user_model()


def _serialize_transfer(t: WalletPeerTransfer) -> dict:
    return {
        "id": t.id,
        "transaction_id": t.transaction_id,
        "amount": str(t.amount),
        "remarks": t.remarks or "",
        "status": t.status,
        "status_display": t.get_status_display(),
        "grant_code": t.grant_code or "",
        "department_id": t.department_id,
        "department_name": t.department.name if t.department_id else "",
        "sender_id": t.sender_id,
        "sender_name": t.sender.name or t.sender.email,
        "sender_email": t.sender.email,
        "recipient_id": t.recipient_id,
        "recipient_name": t.recipient.name or t.recipient.email,
        "recipient_email": t.recipient.email,
        "initiated_by_email": t.initiated_by.email if t.initiated_by_id else "",
        "otp_verified": t.otp_verified,
        "otp_verified_at": t.otp_verified_at.isoformat() if t.otp_verified_at else None,
        "sender_balance_after": str(t.sender_balance_after) if t.sender_balance_after is not None else None,
        "recipient_balance_after": str(t.recipient_balance_after) if t.recipient_balance_after is not None else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "failure_reason": t.failure_reason or "",
    }


def _error_response(exc: PeerTransferError):
    http = status.HTTP_400_BAD_REQUEST
    if exc.code == "forbidden":
        http = status.HTTP_403_FORBIDDEN
    elif exc.code in {"not_found", "recipient_not_found"}:
        http = status.HTTP_404_NOT_FOUND
    return Response({"error": exc.message, "code": exc.code}, status=http)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_peer_transfer_eligible_recipients(request):
    """List recipients eligible under the same grant as the selected department."""
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only Faculty users can initiate wallet-to-wallet transfers.", "code": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    department_id = request.query_params.get("department_id")
    if not department_id:
        return Response({"error": "department_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        department = Department.objects.get(pk=int(department_id), department_type=DepartmentType.INTERNAL)
    except (Department.DoesNotExist, ValueError, TypeError):
        return Response({"error": "Invalid department."}, status=status.HTTP_400_BAD_REQUEST)

    q = (request.query_params.get("q") or "").strip()
    limit = min(int(request.query_params.get("limit") or 20), 50)
    grant = resolve_department_grant_code(department)
    results = list_eligible_recipients(sender=request.user, department=department, query=q, limit=limit)
    return Response({"results": results, "grant_code": grant, "count": len(results)}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_peer_transfer_send_otp(request):
    """Create a pending transfer and email OTP to the initiating faculty user."""
    department_id = request.data.get("department_id")
    recipient_id = request.data.get("recipient_id")
    amount = request.data.get("amount")
    remarks = request.data.get("remarks") or ""

    if not department_id or not recipient_id:
        return Response(
            {"error": "department_id and recipient_id are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        department = Department.objects.get(pk=int(department_id), department_type=DepartmentType.INTERNAL)
    except (Department.DoesNotExist, ValueError, TypeError):
        return Response({"error": "Invalid department."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        recipient = User.objects.select_related("department").get(pk=int(recipient_id))
    except (User.DoesNotExist, ValueError, TypeError):
        return Response(
            {"error": "Recipient not found.", "code": "recipient_not_found"},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        transfer, otp = create_pending_transfer(
            sender=request.user,
            recipient=recipient,
            department=department,
            amount=amount,
            remarks=remarks,
        )
        send_transfer_otp_email(transfer, otp)
    except PeerTransferError as exc:
        return _error_response(exc)
    except Exception as exc:
        logger.exception("peer transfer send-otp failed")
        return Response({"error": f"Failed to send OTP: {exc}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(
        {
            "message": f"OTP sent to {request.user.email}. Enter it to complete the transfer.",
            "transfer": _serialize_transfer(transfer),
            "otp_expires_at": transfer.otp_expires_at.isoformat() if transfer.otp_expires_at else None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_peer_transfer_confirm(request):
    """Verify OTP and execute the transfer atomically."""
    transfer_id = request.data.get("transfer_id")
    otp = (request.data.get("otp") or request.data.get("user_otp") or "").strip()
    if not transfer_id or not otp:
        return Response(
            {"error": "transfer_id and otp are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        transfer = confirm_transfer_with_otp(
            transfer_id=int(transfer_id),
            sender=request.user,
            otp=otp,
        )
    except PeerTransferError as exc:
        return _error_response(exc)
    except (ValueError, TypeError):
        return Response({"error": "Invalid transfer_id."}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:
        logger.exception("peer transfer confirm failed")
        return Response({"error": f"Transfer failed: {exc}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    try:
        notify_peer_transfer_completed(transfer)
    except Exception:
        logger.exception("peer transfer notify failed for %s", transfer.transaction_id)

    return Response(
        {
            "message": (
                f"Transfer {transfer.transaction_id} completed. "
                f"₹{transfer.amount} sent to {transfer.recipient.name or transfer.recipient.email}."
            ),
            "transfer": _serialize_transfer(transfer),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_peer_transfer_history(request):
    """List peer transfers for the current user (sent or received)."""
    from django.db.models import Q

    qs = (
        WalletPeerTransfer.objects.filter(Q(sender=request.user) | Q(recipient=request.user))
        .select_related("sender", "recipient", "department", "initiated_by")
        .order_by("-created_at")
    )
    status_filter = (request.query_params.get("status") or "").strip().upper()
    if status_filter:
        qs = qs.filter(status=status_filter)
    rows = list(qs[:100])
    return Response(
        {"transfers": [_serialize_transfer(t) for t in rows], "count": len(rows)},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_peer_transfer_source_departments(request):
    """Departments (sub-wallets) the faculty can transfer from, with balances and grant codes."""
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only Faculty users can initiate wallet-to-wallet transfers.", "code": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    wallet = request.user.get_accessible_wallet()
    if not wallet or wallet.user_id != request.user.id:
        return Response({"departments": [], "count": 0}, status=status.HTTP_200_OK)

    rows = []
    for sw in SubWallet.objects.filter(wallet=wallet).select_related("department").order_by("department__name"):
        dept = sw.department
        if not dept or dept.department_type != DepartmentType.INTERNAL:
            continue
        rows.append(
            {
                "id": dept.id,
                "name": dept.name,
                "code": dept.code or "",
                "grant_code": resolve_department_grant_code(dept),
                "balance": str(sw.balance),
            }
        )
    return Response({"departments": rows, "count": len(rows)}, status=status.HTTP_200_OK)
