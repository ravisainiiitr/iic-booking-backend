"""Pending approvals for the signed-in staff member (shown as a login popup)."""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)


def _scoped(qs, field: str, equipment_ids):
    if equipment_ids is None:
        return qs
    if not equipment_ids:
        return qs.none()
    return qs.filter(**{f"{field}__in": equipment_ids})


def collect_pending_actions(user) -> list[dict[str, Any]]:
    from .api_views import _get_equipment_ids_for_log_access, check_operator_permission
    from .models import (
        EquipmentPublicationClaim,
        EquipmentPublicationClaimStatus,
        OperatorLeaveRequest,
        RepeatSampleRequest,
        RepeatSampleRequestStatus,
        UrgentBookingRequest,
        UrgentBookingRequestStatus,
    )

    user_type = getattr(user, "user_type", None)
    items: list[dict[str, Any]] = []

    def add(key: str, label: str, count: int, link: str, description: str) -> None:
        if count:
            items.append({"key": key, "label": label, "count": count, "link": link, "description": description})

    is_staff_manager = check_operator_permission(user)
    equipment_ids = _get_equipment_ids_for_log_access(user) if is_staff_manager else []

    if is_staff_manager:
        add(
            "repeat_sample_requests",
            "Repeat sample requests",
            _scoped(
                RepeatSampleRequest.objects.filter(status=RepeatSampleRequestStatus.PENDING),
                "booking__equipment_id",
                equipment_ids,
            ).count(),
            "/repeat-sample-requests",
            "Users have asked for a complimentary repeat sample. Approve or reject each request.",
        )
        if user_type != UserType.OPERATOR:
            add(
                "urgent_requests",
                "Urgent booking requests",
                _scoped(
                    UrgentBookingRequest.objects.filter(status=UrgentBookingRequestStatus.PENDING),
                    "equipment_id",
                    equipment_ids,
                ).count(),
                "/urgent-requests",
                "Urgent (Type B) booking requests are waiting for your decision.",
            )

    if user_type in (UserType.MANAGER, UserType.ADMIN) and getattr(user, "department_id", None):
        add(
            "leave_requests",
            "Lab Incharge leave requests",
            OperatorLeaveRequest.objects.filter(
                status=OperatorLeaveRequest.Status.PENDING, operator__department_id=user.department_id
            ).count(),
            "/oic-leave-management",
            "Leave requests from Lab Incharges in your department need approval.",
        )

    try:
        from .publication_claim_views import _filter_review_queryset, _is_admin, _managed_equipment_ids

        if _is_admin(user) or (user_type == UserType.MANAGER and _managed_equipment_ids(user)):
            add(
                "publication_claims",
                "Publication claims",
                _filter_review_queryset(
                    user, EquipmentPublicationClaim.objects.filter(status=EquipmentPublicationClaimStatus.PENDING)
                ).count(),
                "/publication-claims",
                "Publication claims are waiting for your review.",
            )
    except Exception:
        logger.exception("pending actions: publication claims count failed user_id=%s", user.id)

    if user_type == UserType.ADMIN:
        from iic_booking.communication.models import Notice

        add(
            "notice_requests",
            "Notice board requests",
            Notice.objects.filter(approval_status=Notice.ApprovalStatus.PENDING).count(),
            "/admin-settings/communication?tab=notices",
            "Notice board requests from Officers in charge need approval.",
        )

    return items


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def pending_actions(request):
    items = collect_pending_actions(request.user)
    return Response({"items": items, "total": sum(i["count"] for i in items)}, status=status.HTTP_200_OK)
