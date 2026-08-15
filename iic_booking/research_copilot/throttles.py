"""DRF throttles scoped to Research Copilot (do not throttle the whole portal)."""

from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle


class ResearchCopilotUserThrottle(SimpleRateThrottle):
    """Per-user throttle for authenticated Copilot chat / conversations / tools."""

    scope = "research_copilot_user"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class ResearchCopilotAnonThrottle(SimpleRateThrottle):
    """Per-IP throttle for anonymous Public Copilot (AI.24.1).

    Prevents unlimited anonymous Ollama consumption while bookings stay higher priority.
    """

    scope = "research_copilot_anon"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            return None
        ident = self.get_ident(request)
        if not ident:
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}


class ResearchCopilotToolThrottle(SimpleRateThrottle):
    """Stricter per-user throttle for /tools/execute/."""

    scope = "research_copilot_tool"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class ResearchCopilotAnonToolThrottle(SimpleRateThrottle):
    """Stricter per-IP throttle for anonymous tool execute."""

    scope = "research_copilot_anon_tool"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            return None
        ident = self.get_ident(request)
        if not ident:
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}
