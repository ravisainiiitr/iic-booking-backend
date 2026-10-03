"""IIC Booking mobile app APIs: audience setting (Main Administrator) and APK distribution (app audience)."""

from __future__ import annotations

import mimetypes
import os

from django.core import signing
from django.db.models import F
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.common_download import build_direct_download_url, build_installer_file_response
from iic_booking.deployment import mobile_app
from iic_booking.deployment.models import MobileAppRelease, MobileAppSettings
from iic_booking.installer_download_tickets import absolute_ticket_url, issue_ticket, parse_ticket
from iic_booking.users.models.user_type import UserType

mimetypes.add_type("application/vnd.android.package-archive", ".apk")

TICKET_PRODUCT = "android_app"
TICKET_SECONDS = 900
APK_MIME = "application/vnd.android.package-archive"


def _is_main_admin(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    return bool(getattr(user, "is_superuser", False)) or getattr(user, "user_type", None) == UserType.ADMIN


def _may_download(user) -> bool:
    return _is_main_admin(user) or mobile_app.user_in_app_audience(user)


def _refusal() -> Response:
    return Response(mobile_app.audience_refusal_payload(), status=status.HTTP_403_FORBIDDEN)


def _latest_release(platform: str = MobileAppRelease.Platform.ANDROID):
    qs = MobileAppRelease.objects.filter(platform=platform, is_active=True)
    return qs.filter(is_latest=True).first() or qs.order_by("-version_code", "-created_at").first()


def _file_size(rel: MobileAppRelease) -> int:
    if rel.download_size_bytes:
        return int(rel.download_size_bytes)
    try:
        return int(rel.file.size) if rel.file else 0
    except Exception:
        return 0


def _file_name(rel: MobileAppRelease) -> str:
    if rel.original_name:
        return rel.original_name
    if rel.file:
        return os.path.basename(rel.file.name)
    return f"IIC-Booking-{rel.version_name}.apk"


def serialize_release(rel: MobileAppRelease) -> dict:
    return {
        "id": str(rel.id),
        "platform": rel.platform,
        "version_name": rel.version_name,
        "version_code": rel.version_code,
        "release_date": rel.release_date.isoformat() if rel.release_date else None,
        "release_notes": rel.release_notes,
        "min_android": rel.min_android,
        "file_name": _file_name(rel),
        "size_bytes": _file_size(rel),
        "sha256": rel.sha256,
        "signing_cert_sha256": rel.signing_cert_sha256,
        "has_file": bool(rel.file),
        "is_latest": rel.is_latest,
    }


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def mobile_app_settings(request):
    """Main Administrator: which user types may sign in through the IIC Booking app."""
    if not _is_main_admin(request.user):
        return Response(
            {"error": "Only the Main Administrator can change mobile app settings."},
            status=status.HTTP_403_FORBIDDEN,
        )
    obj = MobileAppSettings.get_singleton()
    if request.method == "PATCH":
        data = request.data if isinstance(request.data, dict) else {}
        try:
            types = mobile_app.clean_audience(data.get("audience_user_types"))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        obj.audience_user_types = types
        obj.updated_by = request.user
        obj.save(update_fields=["audience_user_types", "updated_by", "updated_at"])
        mobile_app.invalidate_audience_cache()
    types = list(obj.audience_user_types or [])
    latest = _latest_release()
    return Response(
        {
            "audience_user_types": types,
            "choices": mobile_app.selectable_choices(),
            "default_audience_user_types": ["manager", "operator", "admin"],
            "refusal_message": mobile_app.audience_message(types),
            "updated_at": obj.updated_at.isoformat() if obj.updated_at else None,
            "latest_release": serialize_release(latest) if latest else None,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mobile_app_latest(request):
    """Latest published Android build, for the dashboard download card and the in-app update check."""
    if not _may_download(request.user):
        return _refusal()
    rel = _latest_release()
    response = Response({"release": serialize_release(rel) if rel and rel.file else None})
    response["Cache-Control"] = "private, max-age=60"
    return response


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mobile_app_download_ticket(request):
    """Short-lived download link for the latest APK (presigned storage URL when available)."""
    if not _may_download(request.user):
        return _refusal()
    rel = _latest_release()
    if not rel or not rel.file:
        return Response({"detail": "The Android app has not been published yet."}, status=status.HTTP_404_NOT_FOUND)
    token = issue_ticket(
        product=TICKET_PRODUCT,
        release_id=str(rel.id),
        offline=False,
        user_id=getattr(request.user, "pk", None),
    )
    name = _file_name(rel)
    direct = build_direct_download_url(rel.file, download_name=name, expires_in=TICKET_SECONDS)
    if direct:
        MobileAppRelease.objects.filter(pk=rel.pk).update(download_count=F("download_count") + 1)
    response = Response(
        {
            "url": direct or absolute_ticket_url(request, TICKET_PRODUCT, token),
            "direct": bool(direct),
            "expires_in": TICKET_SECONDS,
            "filename": name,
            "size_bytes": _file_size(rel),
            "sha256": rel.sha256,
            "version_name": rel.version_name,
            "version_code": rel.version_code,
        }
    )
    response["Cache-Control"] = "no-store"
    return response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def mobile_app_download_by_ticket(request, token: str):
    try:
        data = parse_ticket(token, max_age=TICKET_SECONDS)
    except signing.SignatureExpired:
        return Response({"detail": "Download link expired. Please try again."}, status=status.HTTP_403_FORBIDDEN)
    except signing.BadSignature:
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)
    if data.get("p") != TICKET_PRODUCT:
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)
    rel = MobileAppRelease.objects.filter(pk=data["r"], is_active=True).first()
    if not rel or not rel.file:
        return Response({"detail": "App release not found."}, status=status.HTTP_404_NOT_FOUND)
    MobileAppRelease.objects.filter(pk=rel.pk).update(download_count=F("download_count") + 1)
    response = build_installer_file_response(
        rel.file,
        download_name=_file_name(rel),
        default_name="IIC-Booking.apk",
        sha256=rel.sha256,
        version=rel.version_name,
        size_bytes=_file_size(rel) or None,
        prefer_redirect=True,
    )
    if not response.has_header("Location"):
        response["Content-Type"] = APK_MIME
    return response
