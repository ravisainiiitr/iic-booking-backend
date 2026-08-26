"""Phase B/C mutation package — booking wrappers + wallet scaffolds (flags OFF by default)."""

from __future__ import annotations

from django.conf import settings


def mutations_enabled() -> bool:
    """True if any booking/wallet mutation flag is on (should stay False until controlled enablement)."""
    return bool(
        getattr(settings, "COPILOT_BOOKING_CREATE", False)
        or getattr(settings, "COPILOT_BOOKING_CANCEL", False)
        or getattr(settings, "COPILOT_BOOKING_RESCHEDULE", False)
        or getattr(settings, "COPILOT_BOOKING_MODIFY", False)
        or getattr(settings, "COPILOT_WALLET_RECHARGE", False)
        or getattr(settings, "COPILOT_WALLET_CREDIT", False)
    )


def booking_mutations_enabled() -> bool:
    return bool(
        getattr(settings, "COPILOT_BOOKING_CREATE", False)
        or getattr(settings, "COPILOT_BOOKING_CANCEL", False)
        or getattr(settings, "COPILOT_BOOKING_RESCHEDULE", False)
    )
