"""
Mobile app device-session endpoints (enroll, refresh, logout, device list, revoke).

The web keeps its single DRF Token; these sessions are per device and survive a web login
elsewhere. Tokens appear only in response bodies, always with Cache-Control: no-store.
"""

from __future__ import annotations

import hashlib
import re

from django.db.models import F
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from iic_booking.users import mobile_sessions as ms
from iic_booking.users.models import MobileDeviceSession

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
PLATFORMS = {MobileDeviceSession.PLATFORM_ANDROID, MobileDeviceSession.PLATFORM_IOS}


class MobileEnrollThrottle(SimpleRateThrottle):
    scope = "mobile_enroll"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class MobileRefreshDeviceThrottle(SimpleRateThrottle):
    """Per device: keyed by sha256(device_id) so raw ids never land in the cache."""

    scope = "mobile_refresh_device"

    def get_cache_key(self, request, view):
        data = request.data if isinstance(request.data, dict) else {}
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            return None
        ident = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": ident}


class MobileRefreshIPThrottle(SimpleRateThrottle):
    """Per client IP. Campus traffic shares a NAT, so the configured rate is generous."""

    scope = "mobile_refresh_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


def _no_store(response: Response) -> Response:
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def _error(code: str, message: str, http_status: int, **extra) -> Response:
    return _no_store(Response({"code": code, "error": message, **extra}, status=http_status))


def _current_session_id(request) -> int | None:
    return request.auth.pk if ms.is_mobile_session(request.auth) else None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([MobileEnrollThrottle])
def mobile_enroll(request):
    """
    Register this phone for "stay signed in". Requires a fresh web login (regular Token).

    Body: device_id, platform ("android"|"ios"), device_name (optional), app_version (optional).
    """
    if ms.is_mobile_session(request.auth) or not isinstance(request.auth, Token):
        return _error(
            "ENROLL_REQUIRES_LOGIN",
            "Sign in with your password, OTP or Channel i to set up this device.",
            status.HTTP_403_FORBIDDEN,
        )
    if not ms.sessions_enabled():
        return _error("MOBILE_SESSIONS_DISABLED", "Staying signed in on the app is currently turned off.", status.HTTP_403_FORBIDDEN)

    if ms.login_too_old_to_enroll(getattr(request.auth, "created", None)):
        return _error("LOGIN_TOO_OLD", "Please sign in again to set up this device.", status.HTTP_403_FORBIDDEN)

    data = request.data if isinstance(request.data, dict) else {}
    device_id = str(data.get("device_id") or "").strip()
    platform = str(data.get("platform") or "").strip().lower()
    device_name = str(data.get("device_name") or "").strip()
    app_version = str(data.get("app_version") or "").strip()

    if not DEVICE_ID_RE.match(device_id):
        return _error(
            "INVALID_REQUEST",
            "device_id must be 8-128 characters of letters, digits, '-' or '_'.",
            status.HTTP_400_BAD_REQUEST,
            field="device_id",
        )
    if platform not in PLATFORMS:
        return _error("INVALID_REQUEST", "platform must be 'android' or 'ios'.", status.HTTP_400_BAD_REQUEST, field="platform")
    if len(device_name) > 100:
        return _error("INVALID_REQUEST", "device_name must be at most 100 characters.", status.HTTP_400_BAD_REQUEST, field="device_name")
    if len(app_version) > 32:
        return _error("INVALID_REQUEST", "app_version must be at most 32 characters.", status.HTTP_400_BAD_REQUEST, field="app_version")

    issued = ms.enroll_device(
        request.user,
        device_id=device_id,
        platform=platform,
        device_name=device_name,
        app_version=app_version,
        ip=ms.client_ip(request),
    )
    return _no_store(Response(issued.as_response(), status=status.HTTP_201_CREATED))


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([MobileRefreshDeviceThrottle, MobileRefreshIPThrottle])
def mobile_refresh(request):
    """Rotate tokens. Body: device_id, refresh_token. Same response shape as enroll."""
    if not ms.sessions_enabled():
        return _error("MOBILE_SESSIONS_DISABLED", "Staying signed in on the app is currently turned off.", status.HTTP_403_FORBIDDEN)
    data = request.data if isinstance(request.data, dict) else {}
    device_id = str(data.get("device_id") or "").strip()
    refresh_token = str(data.get("refresh_token") or "").strip()
    try:
        issued = ms.refresh_session(device_id=device_id, refresh_token=refresh_token, ip=ms.client_ip(request))
    except ms.MobileSessionError as exc:
        return _error(exc.code, exc.message, exc.status)
    return _no_store(Response(issued.as_response(), status=status.HTTP_200_OK))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mobile_logout(request):
    """Sign out this device. A no-op (200) when called with a web Token."""
    if ms.is_mobile_session(request.auth):
        ms.revoke_session(request.auth, ms.REASON_LOGOUT)
        return _no_store(Response({"message": "Signed out on this device."}, status=status.HTTP_200_OK))
    return _no_store(Response({"message": "No device session to sign out."}, status=status.HTTP_200_OK))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def mobile_devices(request):
    """The signed-in user's active app sessions, most recently used first."""
    current_id = _current_session_id(request)
    sessions = ms.active_sessions_qs(request.user).order_by(
        F("last_used_at").desc(nulls_last=True), "-created_at"
    )
    results = [
        {
            "id": s.pk,
            "device_name": s.device_name,
            "platform": s.platform,
            "app_version": s.app_version,
            "created_at": s.created_at.isoformat(),
            "last_used_at": s.last_used_at.isoformat() if s.last_used_at else None,
            "refresh_expires_at": s.refresh_expires_at.isoformat(),
            "require_biometric": s.require_biometric,
            "is_current": s.pk == current_id,
        }
        for s in sessions
    ]
    return _no_store(Response({"results": results}, status=status.HTTP_200_OK))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mobile_device_revoke(request, pk: int):
    session = MobileDeviceSession.objects.filter(pk=pk, user=request.user, revoked_at__isnull=True).first()
    if session is None:
        return _error("NOT_FOUND", "Device session not found.", status.HTTP_404_NOT_FOUND)
    ms.revoke_session(session, ms.REASON_USER_REVOKED)
    return _no_store(Response({"message": "Device signed out.", "id": session.pk}, status=status.HTTP_200_OK))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mobile_devices_revoke_all(request):
    """Sign out every app session of this user except the one making the request."""
    count = ms.revoke_user_sessions(
        request.user, ms.REASON_USER_REVOKED_ALL, exclude_session_id=_current_session_id(request)
    )
    return _no_store(
        Response({"message": f"Signed out {count} other device(s).", "revoked": count}, status=status.HTTP_200_OK)
    )
