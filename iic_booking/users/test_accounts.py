"""Helpers for flagged test accounts (seeded QA users)."""

from __future__ import annotations

import re
from typing import Any, Optional

from django.conf import settings
from django.db.models import Q, QuerySet


DEFAULT_TEST_EMAIL_REDIRECT = "ravisaini.15@gmail.com"
TEST_USER_PASSWORD = "Test@IIC2026!"
TEST_EMAIL_DOMAIN = "iic-booking.test"


def parse_email_list(raw: str | None) -> list[str]:
    """Parse one-per-line or comma/semicolon/whitespace-separated emails."""
    if not raw or not str(raw).strip():
        return []
    parts = re.split(r"[\s,;]+", str(raw).strip())
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        addr = p.strip()
        if not addr or "@" not in addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def test_email_redirects() -> list[str]:
    """
    Addresses that receive mail for is_test_account users.
    Prefer Django admin TestAccountEmailSettings; else env TEST_ACCOUNT_EMAIL_REDIRECT
    (comma/semicolon/newline separated); else DEFAULT_TEST_EMAIL_REDIRECT.
    """
    try:
        from iic_booking.users.models.test_account_email_settings import TestAccountEmailSettings

        configured = parse_email_list(TestAccountEmailSettings.get_singleton().recipient_emails)
        if configured:
            return configured
    except Exception:
        # DB unavailable during early migrate / tests without table
        pass

    env_raw = getattr(settings, "TEST_ACCOUNT_EMAIL_REDIRECT", None) or ""
    from_env = parse_email_list(env_raw)
    if from_env:
        return from_env
    return parse_email_list(DEFAULT_TEST_EMAIL_REDIRECT)


def test_email_redirect() -> str:
    """Primary redirect address (first configured). Prefer test_email_redirects()."""
    addrs = test_email_redirects()
    return addrs[0] if addrs else ""


def is_test_user(user: Any) -> bool:
    if user is None:
        return False
    return bool(getattr(user, "is_test_account", False))


def booking_is_test(booking: Any) -> bool:
    """A booking is test data when the booked user is a test account."""
    if booking is None:
        return False
    user = getattr(booking, "user", None)
    if user is not None:
        return is_test_user(user)
    # Avoid N+1 when only user_id is loaded and user was not select_related.
    user_id = getattr(booking, "user_id", None)
    if not user_id:
        return False
    from iic_booking.users.models import User

    return User.objects.filter(pk=user_id, is_test_account=True).exists()


def exclude_test_bookings(qs: QuerySet) -> QuerySet:
    return qs.exclude(user__is_test_account=True)


def exclude_test_wallet_txns(qs: QuerySet) -> QuerySet:
    return qs.exclude(
        Q(sub_wallet__wallet__user__is_test_account=True)
        | Q(related_user__is_test_account=True)
    )


def redirect_email_for_user(
    user: Any,
    *,
    original_email: Optional[str] = None,
    subject: Optional[str] = None,
) -> tuple[list[str], Optional[str]]:
    """
    Return (delivery_emails, maybe_prefixed_subject).
    If user is not a test account, delivery_emails is the original address (0 or 1 item).
    If user is a test account, delivery_emails are all configured redirect addresses.
    """
    email = (original_email or getattr(user, "email", None) or "").strip()
    if not is_test_user(user):
        return ([email] if email else [], subject)
    redirects = test_email_redirects()
    if not redirects:
        return ([email] if email else [], subject)
    new_subject = subject
    if subject is not None:
        tag = f"[TEST:{email or 'unknown'}] "
        if not str(subject).startswith("[TEST:"):
            new_subject = tag + str(subject)
    return redirects, new_subject


def redirect_email_address(email: str, *, subject: Optional[str] = None) -> tuple[list[str], Optional[str]]:
    """
    If the given address belongs to a test account, redirect delivery to configured list.
    Safe to call with bare email strings from bypass paths.
    """
    addr = (email or "").strip()
    if not addr:
        return [], subject
    from iic_booking.users.models import User

    user = User.objects.filter(email__iexact=addr).only("id", "email", "is_test_account").first()
    if not user or not is_test_user(user):
        return [addr], subject
    return redirect_email_for_user(user, original_email=addr, subject=subject)


def test_user_email_for_type(user_type_code: str) -> str:
    safe = str(user_type_code or "unknown").strip().lower().replace(" ", "_")
    return f"test.{safe}@{TEST_EMAIL_DOMAIN}"
