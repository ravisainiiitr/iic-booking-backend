"""Helpers for flagged test accounts (seeded QA users)."""

from __future__ import annotations

import re
from typing import Any, Optional

from django.conf import settings
from django.db.models import Q, QuerySet


TEST_USER_PASSWORD = "Test@IIC2026!"
TEST_EMAIL_DOMAIN = "iic-booking.test"

# Always route OTPs (and other mail) for these seeded accounts to the redirect target.
FORCE_EMAIL_REDIRECT_ADDRESSES = frozenset(
    {
        "test.student@iic-booking.test",
        "test.faculty@iic-booking.test",
    }
)


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


def email_redirects() -> list[str]:
    """
    Addresses that receive mail for is_test_account users.
    Prefer Django admin TestAccountEmailSettings; else env TEST_ACCOUNT_EMAIL_REDIRECT
    (comma/semicolon/newline separated); else none (no address may be hard-coded: public repository).
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
    return parse_email_list(env_raw)


def email_redirect() -> str:
    """Primary redirect address (first configured). Prefer email_redirects()."""
    addrs = email_redirects()
    return addrs[0] if addrs else ""


def is_test_user(user: Any) -> bool:
    if user is None:
        return False
    if bool(getattr(user, "is_test_account", False)):
        return True
    email = (getattr(user, "email", None) or "").strip().lower()
    return email in FORCE_EMAIL_REDIRECT_ADDRESSES


def should_force_email_redirect(email: str | None) -> bool:
    addr = (email or "").strip().lower()
    return bool(addr) and addr in FORCE_EMAIL_REDIRECT_ADDRESSES


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


def user_may_see_test_only_equipment(user: Any) -> bool:
    """Equipment flagged visible_to_test_accounts_only: test accounts and the Main Administrator only."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if is_test_user(user) or getattr(user, "is_superuser", False):
        return True
    from iic_booking.users.models.user_type import UserType

    return getattr(user, "user_type", None) == UserType.ADMIN


def exclude_test_only_equipment(qs: QuerySet, user: Any = None, *, prefix: str = "") -> QuerySet:
    """Drop test-only equipment from ``qs`` unless ``user`` may see it (``prefix`` e.g. ``"equipment__"``)."""
    if user_may_see_test_only_equipment(user):
        return qs
    return qs.exclude(**{f"{prefix}visible_to_test_accounts_only": True})


def exclude_test_bookings(qs: QuerySet) -> QuerySet:
    return qs.exclude(user__is_test_account=True)


def exclude_test_wallet_txns(qs: QuerySet) -> QuerySet:
    return qs.exclude(
        Q(sub_wallet__wallet__user__is_test_account=True)
        | Q(related_user__is_test_account=True)
    )


TEST_NOT_COUNTED_LABEL = "Test — not counted in revenue"


def test_wallet_member_ids() -> QuerySet:
    """Users currently linked (approved) to a test account's wallet: their bookings are paid from that test wallet."""
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    return WalletJoinRequest.objects.filter(
        status=WalletJoinRequestStatus.APPROVED, wallet__user__is_test_account=True
    ).values("student_id")


def exclude_test_revenue_bookings(qs: QuerySet) -> QuerySet:
    """Bookings that count as revenue: not by a test account and not paid from a test account's wallet."""
    return exclude_test_bookings(qs).exclude(user_id__in=test_wallet_member_ids())


def exclude_test_recharge_requests(qs: QuerySet) -> QuerySet:
    return qs.exclude(user__is_test_account=True)


def recharge_request_is_test(recharge_request: Any) -> bool:
    """Recharges by a test account carry no real money: no SRIC cash-book entry is expected for them."""
    return is_test_user(getattr(recharge_request, "user", None))


def filter_by_test_param(qs: QuerySet, value: Any, *, test_q: Q) -> QuerySet:
    """``test=hide`` drops rows matching ``test_q``; ``test=only`` keeps only them; anything else keeps all."""
    choice = str(value or "").strip().lower()
    if choice == "hide":
        return qs.exclude(test_q)
    if choice == "only":
        return qs.filter(test_q)
    return qs


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
    redirects = email_redirects()
    if not redirects:
        return ([email] if email else [], subject)
    return redirects, subject


def redirect_email_address(email: str, *, subject: Optional[str] = None) -> tuple[list[str], Optional[str]]:
    """
    If the given address belongs to a test account, redirect delivery to configured list.
    Safe to call with bare email strings from bypass paths.
    """
    addr = (email or "").strip()
    if not addr:
        return [], subject
    if should_force_email_redirect(addr):
        # Seeded test logins have undeliverable addresses: with no redirect configured, send nothing.
        return email_redirects(), subject
    from iic_booking.users.models import User

    user = User.objects.filter(email__iexact=addr).only("id", "email", "is_test_account").first()
    if not user or not is_test_user(user):
        return [addr], subject
    return redirect_email_for_user(user, original_email=addr, subject=subject)


def user_email_for_type(user_type_code: str) -> str:
    safe = str(user_type_code or "unknown").strip().lower().replace(" ", "_")
    return f"test.{safe}@{TEST_EMAIL_DOMAIN}"
