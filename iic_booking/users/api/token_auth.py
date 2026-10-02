"""
Custom token authentication. Token does not expire due to inactivity.
Single session is still enforced at login (token is regenerated on each login).

Keys starting with "iicm_" are mobile app device-session access tokens
(see iic_booking.users.mobile_sessions); they are independent of the web Token.
"""
from django.conf import settings
from rest_framework import authentication, exceptions
from rest_framework.authtoken.models import Token

MOBILE_ACCESS_PREFIX = "iicm_"


def set_token_activity(token_key):
    """No-op: inactivity expiry is disabled. Kept for API compatibility with login views."""
    pass


def get_inactivity_timeout_seconds(user=None) -> int:
    """
    Backwards-compatible helper for the frontend.

    Tokens do not actually expire due to inactivity, but some clients expect this value
    to be present on `/api/auth/user/`.
    """
    try:
        return int(getattr(settings, "AUTH_INACTIVITY_TIMEOUT_SECONDS", 1800))
    except (TypeError, ValueError):
        return 1800


def _user_for_key(key, request=None):
    """Active user for a web Token key or a mobile access key, else None."""
    if not key:
        return None
    if key.startswith(MOBILE_ACCESS_PREFIX):
        from iic_booking.users.mobile_sessions import authenticate_mobile_access_key, client_ip

        result = authenticate_mobile_access_key(key, ip=client_ip(request))
        return result[0] if result else None
    try:
        token = Token.objects.select_related("user").get(key=key)
    except Token.DoesNotExist:
        return None
    return token.user if token.user.is_active else None


def resolve_request_user(request):
    """
    Return the authenticated user from session/auth header, or from ?token= query param.

    Used for media endpoints loaded via <img src> where Authorization headers are not sent.
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user

    auth_header = (request.META.get("HTTP_AUTHORIZATION") or "").strip()
    if auth_header.lower().startswith("token "):
        header_key = auth_header[6:].strip()
        if header_key:
            header_user = _user_for_key(header_key, request)
            if header_user is not None:
                return header_user

    token_key = (getattr(request, "query_params", None) or {}).get("token") or request.GET.get("token") or ""
    token_key = (token_key or "").strip()
    if not token_key:
        return None

    return _user_for_key(token_key, request)


class TokenAuthenticationWithInactivity(authentication.TokenAuthentication):
    """
    Token authentication. Tokens do not expire due to inactivity.
    """

    def authenticate(self, request):
        auth = authentication.get_authorization_header(request).split()
        if not auth or auth[0].lower() != b"token":
            return None

        if len(auth) == 1:
            raise exceptions.AuthenticationFailed("Invalid token header. No credentials provided.")
        if len(auth) > 2:
            raise exceptions.AuthenticationFailed("Invalid token header. Token string should not contain spaces.")

        try:
            key = auth[1].decode("utf-8")
        except UnicodeError:
            raise exceptions.AuthenticationFailed("Invalid token header. Token string should not contain invalid characters.")

        if key.startswith(MOBILE_ACCESS_PREFIX):
            from iic_booking.users.mobile_sessions import authenticate_mobile_access_key, client_ip

            result = authenticate_mobile_access_key(key, ip=client_ip(request))
            if result is None:
                raise exceptions.AuthenticationFailed("Invalid token.")
            return result

        try:
            token = Token.objects.select_related("user").get(key=key)
        except Token.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid token.")

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("User inactive or deleted.")

        return (token.user, token)


class OptionalTokenAuthentication(TokenAuthenticationWithInactivity):
    """
    Token auth for installer endpoints that allow anonymous fallback.

    Invalid or stale ``Authorization: Token …`` must not return HTTP 401 — treat as
    anonymous so provisioning session create can proceed (pending approval path).
    """

    def authenticate(self, request):
        auth = authentication.get_authorization_header(request).split()
        if not auth or auth[0].lower() != b"token":
            return None

        try:
            return super().authenticate(request)
        except exceptions.AuthenticationFailed:
            return None
