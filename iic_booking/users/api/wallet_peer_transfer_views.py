"""Faculty wallet-to-wallet transfers: history only.

Starting a transfer was removed after the 31 Oct 2026 committee meeting. Transfers already made stay in each
user's history (and in the wallet ledger) unchanged.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models import WalletPeerTransfer
from iic_booking.users.display import get_user_display_name

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
        "sender_name": get_user_display_name(t.sender),
        "sender_email": t.sender.email,
        "recipient_id": t.recipient_id,
        "recipient_name": get_user_display_name(t.recipient),
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
