"""Phase B/C mutation scaffolds — flags OFF until Phase A acceptance + enablement gates."""

from __future__ import annotations

from django.conf import settings


def mutations_enabled() -> bool:
    """Master gate: never true unless an explicit booking/wallet flag is on."""
    return bool(
        getattr(settings, "COPILOT_BOOKING_CREATE", False)
        or getattr(settings, "COPILOT_BOOKING_CANCEL", False)
        or getattr(settings, "COPILOT_BOOKING_RESCHEDULE", False)
        or getattr(settings, "COPILOT_BOOKING_MODIFY", False)
        or getattr(settings, "COPILOT_WALLET_RECHARGE", False)
        or getattr(settings, "COPILOT_WALLET_CREDIT", False)
    )
