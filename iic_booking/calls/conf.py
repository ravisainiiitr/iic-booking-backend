"""Settings for in-app calls and the WebRTC ICE configuration handed to clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from django.conf import settings


def enabled() -> bool:
    return bool(getattr(settings, "CALLS_ENABLED", False))


def test_accounts_only() -> bool:
    return bool(getattr(settings, "CALLS_TEST_ACCOUNTS_ONLY", True))


def ring_seconds() -> int:
    return int(getattr(settings, "CALLS_RING_SECONDS", 45) or 45)


def turn_enabled() -> bool:
    return bool(getattr(settings, "CALLS_TURN_URLS", None)) and bool(getattr(settings, "CALLS_TURN_SECRET", ""))


def ice_servers(user, *, now: float | None = None) -> list[dict]:
    """Public STUN servers, plus TURN only when configured. TURN credentials follow coturn's use-auth-secret scheme:
    username "<expiry>:<user id>", password base64(HMAC-SHA1(secret, username)), valid CALLS_TURN_TTL_SECONDS."""
    servers = [{"urls": list(getattr(settings, "CALLS_STUN_URLS", []) or [])}] if getattr(settings, "CALLS_STUN_URLS", None) else []
    if turn_enabled():
        expiry = int((now or time.time()) + int(getattr(settings, "CALLS_TURN_TTL_SECONDS", 600)))
        username = f"{expiry}:{user.pk}"
        digest = hmac.new(settings.CALLS_TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()
        servers.append({"urls": list(settings.CALLS_TURN_URLS), "username": username,
                        "credential": base64.b64encode(digest).decode()})
    return servers
