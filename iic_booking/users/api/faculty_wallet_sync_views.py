"""Main Administrator: read or change the faculty login wallet sync deadline."""

from __future__ import annotations

from django.db import DatabaseError
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.legacy_ledger.faculty_wallet_sync_deadline import (
    FacultyWalletSyncDeadlineError,
    faculty_wallet_sync_deadline_status,
    set_faculty_wallet_sync_cutoff,
)
from iic_booking.users.models import UserType


@api_view(["GET", "PUT", "POST"])
@permission_classes([IsAuthenticated])
def faculty_wallet_sync_deadline(request):
    if getattr(request.user, "user_type", None) != UserType.ADMIN:
        return Response(
            {"error": "Only the Main Administrator can view or change the faculty wallet sync deadline."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if request.method == "GET":
        return Response(faculty_wallet_sync_deadline_status())

    data = request.data or {}
    if "cutoff" not in data:
        return Response(
            {"error": "Send cutoff (date and time with timezone, or empty to close the sync now) and a reason."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        set_faculty_wallet_sync_cutoff(actor=request.user, raw_cutoff=data.get("cutoff"), reason=data.get("reason"))
    except FacultyWalletSyncDeadlineError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except DatabaseError:
        return Response(
            {"error": "The deadline setting is not available until database migration users.0131 is applied."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(faculty_wallet_sync_deadline_status())
