"""Main Administrator: map a new-portal user to an old-portal user ID and sync wallet + legacy bookings."""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.legacy_ledger.admin_user_sync import (
    AdminSyncError,
    apply_sync,
    build_sync_preview,
    legacy_candidates_for_user,
    search_new_users,
    serialize_new_user,
    synced_bookings_for_user,
)
from iic_booking.users.legacy_ledger.reader import (
    OldMySQLConnectionError,
    OldMySQLNotConfigured,
    OldMySQLReader,
)
from iic_booking.users.models import User, UserType

logger = logging.getLogger(__name__)


def _forbidden():
    return Response(
        {"error": "Only the Main Administrator can map and sync legacy users."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _is_main_admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.ADMIN


def _get_user(raw) -> User | None:
    try:
        return User.objects.select_related("department", "supervisor").get(pk=int(raw))
    except (TypeError, ValueError, User.DoesNotExist):
        return None


def _parse_legacy_uid(raw) -> int | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _legacy_db_error(exc: Exception):
    if isinstance(exc, OldMySQLNotConfigured):
        return Response(
            {"error": "The old booking database is not configured on the server."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(
        {"error": "Could not reach the old booking database. Try again shortly."},
        status=status.HTTP_502_BAD_GATEWAY,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def legacy_user_sync_search(request):
    if not _is_main_admin(request.user):
        return _forbidden()
    try:
        limit = int(request.query_params.get("limit", 15))
    except (TypeError, ValueError):
        limit = 15
    return Response({"results": search_new_users(request.query_params.get("q", ""), limit)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def legacy_user_sync_user_detail(request, user_id: int):
    """Full new-portal details plus suggested old-portal accounts (same emp/student ID or email)."""
    if not _is_main_admin(request.user):
        return _forbidden()
    user = _get_user(user_id)
    if user is None:
        return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    payload = {
        "user": serialize_new_user(user),
        "synced_bookings": synced_bookings_for_user(user),
        "legacy_candidates": [],
        "legacy_error": None,
    }
    try:
        with OldMySQLReader() as reader:
            payload["legacy_candidates"] = legacy_candidates_for_user(user, reader)
    except (OldMySQLNotConfigured, OldMySQLConnectionError) as exc:
        payload["legacy_error"] = _legacy_db_error(exc).data["error"]
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def legacy_user_sync_preview(request):
    """Test sync (read-only): fetch old balance and bookings, show what confirm would change."""
    if not _is_main_admin(request.user):
        return _forbidden()
    user = _get_user(request.data.get("user_id"))
    if user is None:
        return Response({"error": "Select a user first."}, status=status.HTTP_400_BAD_REQUEST)
    legacy_uid = _parse_legacy_uid(request.data.get("legacy_user_id"))
    if legacy_uid is None:
        return Response({"error": "Enter a valid old-portal user ID."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        with OldMySQLReader() as reader:
            preview = build_sync_preview(
                user,
                legacy_uid,
                wallet_target=(request.data.get("wallet_target") or None),
                reader=reader,
            )
    except (OldMySQLNotConfigured, OldMySQLConnectionError) as exc:
        return _legacy_db_error(exc)
    except AdminSyncError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(preview)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def legacy_user_sync_confirm(request):
    """Confirm mapping and sync. Requires the balance seen in Test sync to still match."""
    if not _is_main_admin(request.user):
        return _forbidden()
    user = _get_user(request.data.get("user_id"))
    if user is None:
        return Response({"error": "Select a user first."}, status=status.HTTP_400_BAD_REQUEST)
    legacy_uid = _parse_legacy_uid(request.data.get("legacy_user_id"))
    if legacy_uid is None:
        return Response({"error": "Enter a valid old-portal user ID."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        with OldMySQLReader() as reader:
            result = apply_sync(
                user,
                legacy_uid,
                actor=request.user,
                reader=reader,
                wallet_target=(request.data.get("wallet_target") or None),
                sync_wallet=bool(request.data.get("sync_wallet", True)),
                sync_bookings=bool(request.data.get("sync_bookings", True)),
                expected_legacy_balance=request.data.get("expected_legacy_balance"),
            )
    except (OldMySQLNotConfigured, OldMySQLConnectionError) as exc:
        return _legacy_db_error(exc)
    except AdminSyncError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    logger.info(
        "Legacy user sync by admin=%s user_id=%s legacy_uid=%s wallet=%s bookings=%s",
        request.user.pk,
        user.pk,
        legacy_uid,
        bool(result.get("wallet")),
        bool(result.get("bookings")),
    )
    result["user"] = serialize_new_user(user)
    result["synced_bookings"] = synced_bookings_for_user(user)
    return Response(result)
