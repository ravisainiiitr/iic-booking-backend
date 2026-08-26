"""Phase B/C mutation package — booking wrappers + wallet scaffolds (flags OFF by default)."""

from __future__ import annotations

from typing import Any

from django.conf import settings


_BOOKING_FLAGS = frozenset(
    {
        "COPILOT_BOOKING_CREATE",
        "COPILOT_BOOKING_CANCEL",
        "COPILOT_BOOKING_RESCHEDULE",
        "COPILOT_BOOKING_MODIFY",
    }
)


def parse_booking_test_user_ids(raw: str | None = None) -> set[int]:
    """Parse COPILOT_BOOKING_TEST_USER_IDS (comma/space/semicolon separated ints)."""
    text = raw if raw is not None else getattr(settings, "COPILOT_BOOKING_TEST_USER_IDS", "") or ""
    out: set[int] = set()
    for part in str(text).replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except (TypeError, ValueError):
            continue
    return out


def is_booking_e2e_test_user(user: Any) -> bool:
    """
    True when controlled E2E mode is ON and the authenticated user is an
    allowlisted is_test_account. Real users are never admitted.
    """
    if not getattr(settings, "COPILOT_BOOKING_E2E_TEST_MODE", False):
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_test_account", False):
        return False
    allow = parse_booking_test_user_ids()
    if not allow:
        # Fail closed: E2E mode without an explicit allowlist enables nobody.
        return False
    try:
        return int(user.pk) in allow
    except (TypeError, ValueError):
        return False


def booking_mutation_allowed(user: Any, flag_name: str) -> bool:
    """
    Gate for booking mutation execute/executable.

    - Global COPILOT_BOOKING_* flag ON → allowed for any authenticated caller
      (production enablement path; keep OFF until Main Admin approval).
    - Else E2E test mode + allowlisted is_test_account → allowed for that
      booking flag only (CREATE/CANCEL/RESCHEDULE). Wallet flags never via E2E.
    """
    if flag_name not in _BOOKING_FLAGS and not str(flag_name).startswith("COPILOT_BOOKING_"):
        return bool(getattr(settings, flag_name, False))

    if bool(getattr(settings, flag_name, False)):
        return True

    # Controlled E2E: booking flags only; never wallet.
    if flag_name in {
        "COPILOT_BOOKING_CREATE",
        "COPILOT_BOOKING_CANCEL",
        "COPILOT_BOOKING_RESCHEDULE",
    } and is_booking_e2e_test_user(user):
        return True
    return False


def mutations_enabled() -> bool:
    """True if any *global* booking/wallet mutation flag is on.

    COPILOT_BOOKING_E2E_TEST_MODE is intentionally excluded: it only admits
    allowlisted is_test_account users and must not look like production-wide enablement.
    """
    return bool(
        getattr(settings, "COPILOT_BOOKING_CREATE", False)
        or getattr(settings, "COPILOT_BOOKING_CANCEL", False)
        or getattr(settings, "COPILOT_BOOKING_RESCHEDULE", False)
        or getattr(settings, "COPILOT_BOOKING_MODIFY", False)
        or getattr(settings, "COPILOT_WALLET_RECHARGE", False)
        or getattr(settings, "COPILOT_WALLET_CREDIT", False)
    )


def booking_mutations_enabled() -> bool:
    """True if any global booking mutation flag is on (excludes E2E allowlist mode)."""
    return bool(
        getattr(settings, "COPILOT_BOOKING_CREATE", False)
        or getattr(settings, "COPILOT_BOOKING_CANCEL", False)
        or getattr(settings, "COPILOT_BOOKING_RESCHEDULE", False)
    )
