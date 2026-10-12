"""Read-only mode for accounts suspended after their programme / employment end date.

A suspended user can still sign in, browse, view past bookings and download results; every other API write is
refused with 403 ``account_suspended``. Only active while ``ACCOUNT_END_DATE_ENFORCEMENT`` is on."""

from __future__ import annotations

import re
import threading
import time

from django.http import JsonResponse

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Writes a suspended user still needs: session / password, asking for the extension, reading notifications,
# settling dues on existing bookings, cancelling them, marking results viewed and contacting support.
ALLOWED_PREFIXES = (
    "/api/auth/",
    "/api/account-lifecycle/",
    "/api/registration-approvals/",
    "/api/notifications/",
    "/api/payments/",
    "/api/peak-window/",
    "/api/version",
    "/api/chat-agent/",
    "/api/exports/",
    "/api/portal-feedback/",
)
ALLOWED_PATTERNS = (
    re.compile(r"^/api/bookings/\d+/results/"),
    re.compile(r"^/api/bookings/\d+/cancel/$"),
    re.compile(r"^/api/bookings/\d+/process-charge-recalculation-pay-now/$"),
    re.compile(r"^/api/wallet/credit-requests/\d+/repay/$"),
    re.compile(r"^/api/tickets/$"),
    re.compile(r"^/api/tickets/\d+/comments/create/$"),
)
# Profile edits (contact details, preferences, guide dismissal) stay possible; file uploads to it do not.
PROFILE_PATH = re.compile(r"^/api/users/\d+/$")

_TOKEN_CACHE_SECONDS = 30
_token_lock = threading.Lock()
_token_suspended: dict[str, tuple[float, object]] = {}


def is_allowed_path(path: str) -> bool:
    return path.startswith(ALLOWED_PREFIXES) or any(p.match(path) for p in ALLOWED_PATTERNS)


def is_allowed_request(request) -> bool:
    if is_allowed_path(request.path):
        return True
    return (
        request.method == "PATCH"
        and PROFILE_PATH.match(request.path) is not None
        and not (request.META.get("CONTENT_TYPE") or "").lower().startswith("multipart/")
    )


def _token_key(request) -> str:
    header = (request.META.get("HTTP_AUTHORIZATION") or "").strip()
    if header.lower().startswith("token "):
        return header[6:].strip()
    return ""


def _user_from_token(key: str):
    from rest_framework.authtoken.models import Token

    from iic_booking.users.mobile_sessions import ACCESS_PREFIX, authenticate_mobile_access_key

    if key.startswith(ACCESS_PREFIX):
        result = authenticate_mobile_access_key(key, touch=False)
        return result[0] if result else None
    token = Token.objects.select_related("user").filter(key=key).first()
    return token.user if token is not None and token.user.is_active else None


def _suspended_user(user):
    from iic_booking.users import account_lifecycle

    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user if account_lifecycle.is_suspended(user) else None


def _suspended_from_token(key: str):
    now = time.monotonic()
    with _token_lock:
        hit = _token_suspended.get(key)
    if hit and hit[0] > now:
        return hit[1]
    user = _suspended_user(_user_from_token(key))
    with _token_lock:
        if len(_token_suspended) > 20000:
            _token_suspended.clear()
        _token_suspended[key] = (now + _TOKEN_CACHE_SECONDS, user)
    return user


def clear_token_cache() -> None:
    with _token_lock:
        _token_suspended.clear()


class AccountSuspensionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        refused = self._refusal(request)
        if refused is not None:
            return refused
        return self.get_response(request)

    def _refusal(self, request):
        if request.method not in WRITE_METHODS or not request.path.startswith("/api/"):
            return None
        from iic_booking.users import account_lifecycle

        if not account_lifecycle.enforcement_enabled() or is_allowed_request(request):
            return None
        try:
            session_user = getattr(request, "user", None)
            if session_user is not None and getattr(session_user, "is_authenticated", False):
                user = _suspended_user(session_user)
            else:
                key = _token_key(request)
                user = _suspended_from_token(key) if key else None
        except Exception:
            # Never take the API down over this check; the booking endpoint re-checks on its own.
            import logging

            logging.getLogger(__name__).exception("account suspension check failed")
            return None
        if user is None:
            return None
        from django.core.cache import cache

        if cache.add(f"account_lifecycle:write_refused:{user.pk}", 1, 3600):
            account_lifecycle.event(
                account_lifecycle.LEvent.Action.WRITE_REFUSED, user=user, method=request.method, path=request.path[:200]
            )
        response = JsonResponse(account_lifecycle.suspension_payload(user), status=403)
        response["Cache-Control"] = "no-store"
        return response
