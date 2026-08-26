"""DRF throttles scoped to Research Copilot (do not throttle the whole portal)."""

from __future__ import annotations

from django.core.cache import cache
from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class ResearchCopilotUserThrottle(SimpleRateThrottle):
    """Legacy alias — now maps to the higher read budget (chat ingress)."""

    scope = "research_copilot_read"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class ResearchCopilotReadThrottle(SimpleRateThrottle):
    """Per-user throttle for Copilot chat / deterministic reads."""

    scope = "research_copilot_read"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class ResearchCopilotToolThrottle(SimpleRateThrottle):
    """Per-user throttle for /tools/execute/ (read tools)."""

    scope = "research_copilot_tool"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class ResearchCopilotMutationThrottle(SimpleRateThrottle):
    """Strict throttle for Phase B/C mutation executes."""

    scope = "research_copilot_mutation"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class ResearchCopilotAnonThrottle(SimpleRateThrottle):
    """IP-based throttle for anonymous Copilot public endpoints."""

    scope = "research_copilot_anon"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        if not ident:
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}


def _parse_rate(rate: str) -> tuple[int, int]:
    """Return (num_requests, duration_seconds) for DRF-style 'N/hour' rates."""
    num, period = rate.split("/")
    num = int(num)
    duration = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400}.get(
        period.lower(), 3600
    )
    return num, duration


def consume_llm_quota(*, user) -> tuple[bool, str]:
    """
    Internal LLM-only quota (separate from chat ingress).

    Returns (allowed, message). Deterministic turns must NOT call this.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return True, ""
    rates = getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {})
    rate = rates.get("research_copilot_llm") or "60/hour"
    try:
        num, duration = _parse_rate(rate)
    except Exception:  # noqa: BLE001
        num, duration = 60, 3600
    key = f"copilot_llm_quota:{getattr(user, 'pk', 'anon')}"
    count = cache.get(key)
    if count is None:
        cache.set(key, 1, duration)
        return True, ""
    if int(count) >= num:
        return False, "Research Copilot AI replies are rate-limited right now. Live portal lookups (slots, wallet, bookings) still work — try a direct question, or wait and retry."
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, int(count) + 1, duration)
    return True, ""
