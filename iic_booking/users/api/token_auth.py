"""
Custom token authentication. Token does not expire due to inactivity.
Single session is still enforced at login (token is regenerated on each login).
"""
from django.conf import settings
from rest_framework import authentication, exceptions
from rest_framework.authtoken.models import Token


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
            try:
                token = Token.objects.select_related("user").get(key=header_key)
            except Token.DoesNotExist:
                pass
            else:
                if token.user.is_active:
                    return token.user

    token_key = (getattr(request, "query_params", None) or {}).get("token") or request.GET.get("token") or ""
    token_key = (token_key or "").strip()
    if not token_key:
        return None

    try:
        token = Token.objects.select_related("user").get(key=token_key)
    except Token.DoesNotExist:
        return None

    if not token.user.is_active:
        return None

    return token.user


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

        key = auth[1].decode("utf-8")

        try:
            token = Token.objects.select_related("user").get(key=key)
        except Token.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid token.")

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("User inactive or deleted.")

        return (token.user, token)
