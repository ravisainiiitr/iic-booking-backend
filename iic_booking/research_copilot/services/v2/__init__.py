"""Copilot V2 package — deterministic intent/tools over portal domain services."""

from __future__ import annotations

from django.conf import settings


def v2_enabled() -> bool:
    return bool(getattr(settings, "COPILOT_V2_ENABLED", True)) and bool(
        getattr(settings, "RESEARCH_COPILOT_ENABLED", False)
    )


def flag(name: str, default: bool = True) -> bool:
    return bool(getattr(settings, name, default))
