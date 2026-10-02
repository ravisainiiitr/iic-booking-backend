"""Refuse API calls from external users while the slot-opening peak window is active."""

from __future__ import annotations

import threading
import time

from django.http import JsonResponse

from .peak_window import compute_peak_state, external_pause_payload, is_peak_blockable_user

# Always reachable so a paused user can read why, sign out, and see when to come back.
EXEMPT_PATHS = frozenset(
    {
        "/api/auth/logout/",
        "/api/auth/mobile/logout/",
        "/api/auth/user/",
        "/api/auth/settings/",
        "/api/peak-window/status/",
        "/api/version/",
        "/api/version",
    }
)
EXEMPT_GET_PATHS = frozenset({"/api/profiles/me/"})

_TOKEN_CACHE_SECONDS = 30
_token_lock = threading.Lock()
_token_blockable: dict[str, tuple[float, bool]] = {}


def _token_key(request) -> str:
    header = (request.META.get("HTTP_AUTHORIZATION") or "").strip()
    if header.lower().startswith("token "):
        return header[6:].strip()
    return (request.GET.get("token") or "").strip()


def _blockable_from_token(key: str) -> bool:
    now = time.monotonic()
    with _token_lock:
        hit = _token_blockable.get(key)
    if hit and hit[0] > now:
        return hit[1]
    from rest_framework.authtoken.models import Token

    from iic_booking.users.mobile_sessions import ACCESS_PREFIX, authenticate_mobile_access_key

    if key.startswith(ACCESS_PREFIX):
        result = authenticate_mobile_access_key(key, touch=False)
        user = result[0] if result else None
    else:
        token = Token.objects.select_related("user").filter(key=key).first()
        user = token.user if token is not None and token.user.is_active else None
    blockable = is_peak_blockable_user(user)
    with _token_lock:
        if len(_token_blockable) > 20000:
            _token_blockable.clear()
        _token_blockable[key] = (now + _TOKEN_CACHE_SECONDS, blockable)
    return blockable


def _request_is_blockable(request) -> bool:
    session_user = getattr(request, "user", None)
    if session_user is not None and getattr(session_user, "is_authenticated", False):
        return is_peak_blockable_user(session_user)
    key = _token_key(request)
    return bool(key) and _blockable_from_token(key)


class PeakWindowExternalBlockMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        blocked = self._blocked_response(request)
        if blocked is not None:
            return blocked
        return self.get_response(request)

    def _blocked_response(self, request):
        path = request.path
        if not path.startswith("/api/") or request.method == "OPTIONS":
            return None
        if path in EXEMPT_PATHS or (request.method in ("GET", "HEAD") and path in EXEMPT_GET_PATHS):
            return None
        state = compute_peak_state()
        if not state["external_access_paused"]:
            return None
        if not _request_is_blockable(request):
            return None
        response = JsonResponse(external_pause_payload(state["_current"]), status=403)
        response["Cache-Control"] = "no-store"
        return response
