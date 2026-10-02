"""Supervisor-set weekly / monthly spending limits for students on the faculty wallet."""

from __future__ import annotations

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.student_spending_limits import (
    SpendingLimitValidationError,
    apply_spending_limit_update,
    limit_summary,
)
from iic_booking.users.display import get_user_display_name


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_student_spending_limits(request):
    """Faculty: limits and current week / month spend for every approved student on their wallet."""
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can view student spending limits."},
            status=status.HTTP_403_FORBIDDEN,
        )
    links = WalletJoinRequest.objects.filter(
        faculty=request.user, status=WalletJoinRequestStatus.APPROVED
    ).order_by("-id")
    return Response({"limits": [limit_summary(link) for link in links]}, status=status.HTTP_200_OK)


@api_view(["GET", "PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def student_spending_limit_detail(request, request_id):
    """Faculty: read or set the spending limit of one approved student on their own wallet."""
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only the student's supervisor can manage spending limits."},
            status=status.HTTP_403_FORBIDDEN,
        )
    link_qs = WalletJoinRequest.objects.filter(
        pk=request_id, faculty=request.user, status=WalletJoinRequestStatus.APPROVED
    )
    if request.method == "GET":
        link = link_qs.first()
        if link is None:
            return Response(
                {"error": "Approved student not found on your wallet."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(limit_summary(link), status=status.HTTP_200_OK)

    with transaction.atomic():
        link = link_qs.select_for_update().first()
        if link is None:
            return Response(
                {"error": "Approved student not found on your wallet."},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            apply_spending_limit_update(link, request.data)
        except SpendingLimitValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    link.refresh_from_db()
    return Response(
        {"message": "Spending limit saved.", "limit": limit_summary(link)},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_student_spending_limit(request):
    """Student: the limit set by their supervisor on the wallet they book from, with remaining amounts."""
    link = (
        WalletJoinRequest.objects.filter(student=request.user, status=WalletJoinRequestStatus.APPROVED)
        .select_related("faculty")
        .order_by("-id")
        .first()
    )
    if link is None or not link.spending_limit_enabled:
        return Response({"spending_limit_enabled": False}, status=status.HTTP_200_OK)
    payload = limit_summary(link)
    payload["supervisor_name"] = get_user_display_name(link.faculty) if link.faculty_id else None
    return Response(payload, status=status.HTTP_200_OK)
