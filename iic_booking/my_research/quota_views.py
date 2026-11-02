"""Main Administrator: My Research storage quotas (Admin Settings > My Research storage).

GET  /api/v1/my-research/admin/quotas/                 default per-user and per-workspace quotas, users with their own
PUT  /api/v1/my-research/admin/quotas/                 {"user_quota_gb", "workspace_quota_gb"}
POST /api/v1/my-research/admin/quotas/users/           {"email", "quota_gb", "note"}: set one user's own quota
DELETE /api/v1/my-research/admin/quotas/users/<id>/    back to the default
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.department_modules.access import is_main_admin

from .models import StorageQuotaSettings, UserStorageQuota
from .services import owner_usage_bytes

MAX_GB = 100_000


def _gb(value, field: str) -> int:
    try:
        gb = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number of GB.")
    if gb < 0 or gb > MAX_GB:
        raise ValueError(f"{field} must be between 0 and {MAX_GB} GB.")
    return gb


def _payload() -> dict:
    s = StorageQuotaSettings.get_solo()
    rows = UserStorageQuota.objects.select_related("user").order_by("user__email")
    return {
        "user_quota_gb": s.user_quota_gb,
        "workspace_quota_gb": s.workspace_quota_gb,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "users": [
            {"id": r.pk, "email": r.user.email, "name": r.user.name or "", "quota_gb": r.quota_gb, "note": r.note,
             "used_bytes": owner_usage_bytes(r.user)}
            for r in rows
        ],
    }


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def quotas(request):
    if not is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can change storage quotas."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "PUT":
        s = StorageQuotaSettings.get_solo()
        try:
            if "user_quota_gb" in request.data:
                s.user_quota_gb = _gb(request.data["user_quota_gb"], "Quota per user")
            if "workspace_quota_gb" in request.data:
                s.workspace_quota_gb = _gb(request.data["workspace_quota_gb"], "Quota per workspace")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        s.updated_by = request.user
        s.save()
    return Response(_payload())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def user_quota_set(request):
    if not is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can change storage quotas."}, status=status.HTTP_403_FORBIDDEN)
    email = str(request.data.get("email") or "").strip()
    user = get_user_model().objects.filter(email__iexact=email).first() if email else None
    if user is None:
        return Response({"error": "No user with that email."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        gb = _gb(request.data.get("quota_gb"), "Quota")
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    UserStorageQuota.objects.update_or_create(
        user=user, defaults={"quota_gb": gb, "note": str(request.data.get("note") or "")[:200], "updated_by": request.user})
    return Response(_payload())


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def user_quota_clear(request, row_id: int):
    if not is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can change storage quotas."}, status=status.HTTP_403_FORBIDDEN)
    UserStorageQuota.objects.filter(pk=row_id).delete()
    return Response(_payload())
