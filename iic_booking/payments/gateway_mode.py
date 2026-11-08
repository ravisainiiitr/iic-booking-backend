"""Razorpay on/off switch and test/live mode, checked before any order is created.

Settings (environment): RAZORPAY_ENABLED, RAZORPAY_MODE ("test" or "live"), RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
RAZORPAY_WEBHOOK_SECRET, and RAZORPAY_LIVE_ALLOWED.

* Nothing is charged unless RAZORPAY_ENABLED is on.
* The key must belong to the mode: Razorpay test keys start ``rzp_test_``, live keys ``rzp_live_``. A mismatch stops
  every new payment, so test and live credentials can never be mixed.
* Live mode also needs RAZORPAY_LIVE_ALLOWED, so turning on live payments is a deliberate second step.
* In test mode only accounts marked as test accounts can pay online, and every order records its mode, so a test
  payment can never be mistaken for real money (test accounts are already left out of revenue reports).

No message here ever contains a key or secret.
"""

from __future__ import annotations

from django.conf import settings

TEST, LIVE = "test", "live"
_PREFIX = {TEST: "rzp_test_", LIVE: "rzp_live_"}


def enabled() -> bool:
    return bool(getattr(settings, "RAZORPAY_ENABLED", False))


def mode() -> str:
    return str(getattr(settings, "RAZORPAY_MODE", TEST) or "").strip().lower()


def key_mode(key_id: str) -> str:
    """test, live or unknown, from the key's prefix."""
    key_id = (key_id or "").strip()
    for name, prefix in _PREFIX.items():
        if key_id.startswith(prefix):
            return name
    return "unknown"


def configuration_problem() -> str:
    """Why online payment cannot be used now, in words safe to show; empty when it can."""
    if not enabled():
        return "Online payment is switched off."
    current = mode()
    if current not in _PREFIX:
        return "Online payment is misconfigured: RAZORPAY_MODE must be test or live."
    key_id = getattr(settings, "RAZORPAY_KEY_ID", "") or ""
    if not key_id or not (getattr(settings, "RAZORPAY_KEY_SECRET", "") or ""):
        return "Razorpay credentials are not configured."
    if key_mode(key_id) != current:
        return f"Online payment is misconfigured: the Razorpay key does not belong to {current} mode."
    if current == LIVE and not bool(getattr(settings, "RAZORPAY_LIVE_ALLOWED", False)):
        return "Live payments have not been authorised on this server."
    return ""


def user_may_pay(user) -> str:
    """Empty when this user may pay online now; otherwise the reason to show."""
    problem = configuration_problem()
    if problem:
        return problem
    restricted = bool(getattr(settings, "RAZORPAY_TEST_MODE_TEST_ACCOUNTS_ONLY", True))
    if mode() == TEST and restricted and not getattr(user, "is_test_account", False):
        return "Online payment is in test mode. Only test accounts can pay online at the moment."
    return ""


def public_status(user=None) -> dict:
    """What the frontend may know: no keys, no secrets."""
    from iic_booking.payments.receipts import checkout_name

    problem = configuration_problem()
    return {
        "checkout_name": checkout_name(),
        "available": not problem and (user is None or not user_may_pay(user)),
        "mode": mode() if mode() in _PREFIX else "",
        "test_mode": mode() == TEST,
        "reason": user_may_pay(user) if user is not None else problem,
    }
