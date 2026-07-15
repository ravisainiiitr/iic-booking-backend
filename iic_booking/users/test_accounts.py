"""Helpers for flagged test accounts (seeded QA users)."""

from __future__ import annotations

from typing import Any, Optional

from django.conf import settings
from django.db.models import Q, QuerySet


DEFAULT_TEST_EMAIL_REDIRECT = "ravisaini.15@gmail.com"
TEST_USER_PASSWORD = "Test@IIC2026!"
TEST_EMAIL_DOMAIN = "iic-booking.test"


def test_email_redirect() -> str:
    return (getattr(settings, "TEST_ACCOUNT_EMAIL_REDIRECT", None) or DEFAULT_TEST_EMAIL_REDIRECT).strip()


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
) -> tuple[str, Optional[str]]:
    """
    Return (delivery_email, maybe_prefixed_subject).
    If user is not a test account, delivery_email is the original address unchanged.
    """
    email = (original_email or getattr(user, "email", None) or "").strip()
    if not is_test_user(user):
        return email, subject
    redirect_to = test_email_redirect()
    if not redirect_to:
        return email, subject
    new_subject = subject
    if subject is not None:
        tag = f"[TEST:{email or 'unknown'}] "
        if not str(subject).startswith("[TEST:"):
            new_subject = tag + str(subject)
    return redirect_to, new_subject


def redirect_email_address(email: str, *, subject: Optional[str] = None) -> tuple[str, Optional[str]]:
    """
    If the given address belongs to a test account, redirect delivery.
    Safe to call with bare email strings from bypass paths.
    """
    addr = (email or "").strip()
    if not addr:
        return addr, subject
    from iic_booking.users.models import User

    user = User.objects.filter(email__iexact=addr).only("id", "email", "is_test_account").first()
    if not user or not is_test_user(user):
        return addr, subject
    return redirect_email_for_user(user, original_email=addr, subject=subject)


def test_user_email_for_type(user_type_code: str) -> str:
    safe = str(user_type_code or "unknown").strip().lower().replace(" ", "_")
    return f"test.{safe}@{TEST_EMAIL_DOMAIN}"
