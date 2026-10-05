"""SRIC office integration API — transfer requests instead of email."""

from __future__ import annotations

import functools
import hmac

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from iic_booking.users.models.payment import SricTransferRequest, SricTransferRequestStatus


def _check_sric_api_key(request) -> bool:
    expected = (getattr(settings, "SRIC_API_KEY", "") or "").strip()
    if not expected:
        return False
    provided = (request.headers.get("X-SRIC-API-Key") or request.META.get("HTTP_X_SRIC_API_KEY") or "").strip()
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _legacy_endpoint_gate(view):
    """Outermost wrapper so a disabled endpoint answers 404 for every method, before DRF method checks."""

    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if not getattr(settings, "SRIC_LEGACY_TRANSFER_ENDPOINT_ENABLED", False):
            raise Http404
        return view(request, *args, **kwargs)

    return wrapped


def _serialize_transfer(tr: SricTransferRequest) -> dict:
    dept = tr.department
    return {
        "id": tr.id,
        "recharge_request_id": tr.wallet_recharge_request_id,
        "grant_code": tr.grant_code,
        "amount": str(tr.amount),
        "department_id": dept.id if dept else None,
        "department_name": dept.name if dept else "",
        "department_code": dept.code if dept else "",
        "faculty_emp_id": tr.faculty_emp_id,
        "faculty_email": tr.faculty_email,
        "faculty_name": tr.faculty_name,
        "project_code": tr.project_code,
        "project_name": tr.project_name,
        "status": tr.status,
        "sric_reference": tr.sric_reference,
        "transferred_at": tr.transferred_at.isoformat() if tr.transferred_at else None,
        "created_at": tr.created_at.isoformat() if tr.created_at else None,
    }


@_legacy_endpoint_gate
@api_view(["GET"])
@permission_classes([AllowAny])
def sric_transfer_requests_list(request):
    """List pending transfer requests for SRIC office systems."""
    if not _check_sric_api_key(request):
        return Response({"error": "Unauthorized."}, status=status.HTTP_401_UNAUTHORIZED)

    qs = SricTransferRequest.objects.select_related("department", "wallet_recharge_request").order_by("created_at")
    st = (request.query_params.get("status") or "PENDING").strip().upper()
    if st != "ALL":
        qs = qs.filter(status=st)
    dept_id = request.query_params.get("department_id")
    if dept_id:
        qs = qs.filter(department_id=int(dept_id))
    return Response(
        {"transfer_requests": [_serialize_transfer(t) for t in qs[:1000]], "count": qs.count()},
        status=status.HTTP_200_OK,
    )


@_legacy_endpoint_gate
@api_view(["GET"])
@permission_classes([AllowAny])
def sric_transfer_request_detail(request, transfer_id: int):
    if not _check_sric_api_key(request):
        return Response({"error": "Unauthorized."}, status=status.HTTP_401_UNAUTHORIZED)
    tr = get_object_or_404(SricTransferRequest.objects.select_related("department"), pk=transfer_id)
    return Response(_serialize_transfer(tr))


@_legacy_endpoint_gate
@api_view(["POST"])
@permission_classes([AllowAny])
def sric_transfer_request_complete(request, transfer_id: int):
    """SRIC marks transfer as completed."""
    if not _check_sric_api_key(request):
        return Response({"error": "Unauthorized."}, status=status.HTTP_401_UNAUTHORIZED)

    tr = get_object_or_404(SricTransferRequest, pk=transfer_id)
    if tr.status != SricTransferRequestStatus.PENDING:
        return Response({"error": "Not pending."}, status=status.HTTP_400_BAD_REQUEST)

    tr.status = SricTransferRequestStatus.TRANSFERRED
    tr.sric_reference = (request.data.get("sric_reference") or request.data.get("reference") or "")[:128]
    tr.transferred_at = timezone.now()
    tr.save(update_fields=["status", "sric_reference", "transferred_at", "updated_at"])

    return Response({"message": "Transfer recorded.", "transfer": _serialize_transfer(tr)})


@_legacy_endpoint_gate
@api_view(["POST"])
@permission_classes([AllowAny])
def sric_transfer_request_reject(request, transfer_id: int):
    if not _check_sric_api_key(request):
        return Response({"error": "Unauthorized."}, status=status.HTTP_401_UNAUTHORIZED)

    tr = get_object_or_404(SricTransferRequest, pk=transfer_id)
    if tr.status != SricTransferRequestStatus.PENDING:
        return Response({"error": "Not pending."}, status=status.HTTP_400_BAD_REQUEST)

    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return Response({"error": "reason is required."}, status=status.HTTP_400_BAD_REQUEST)

    tr.status = SricTransferRequestStatus.REJECTED
    tr.rejection_reason = reason[:2000]
    tr.save(update_fields=["status", "rejection_reason", "updated_at"])
    return Response({"message": "Rejected.", "transfer": _serialize_transfer(tr)})
