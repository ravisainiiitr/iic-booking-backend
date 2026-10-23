"""Typed access to the I-STEM settings. Reads Django settings only; never touches the network."""

from django.conf import settings

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_SHADOW, MODE_LIVE)


def istem_enabled() -> bool:
    return bool(getattr(settings, "ISTEM_ENABLED", False))


def istem_mode() -> str:
    """``off`` unless the integration is enabled and ``ISTEM_MODE`` names a known mode."""
    if not istem_enabled():
        return MODE_OFF
    mode = str(getattr(settings, "ISTEM_MODE", MODE_OFF) or "").strip().lower()
    return mode if mode in MODES else MODE_OFF
