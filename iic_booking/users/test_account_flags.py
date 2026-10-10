"""Mark / unmark test accounts and report which accounts look like test accounts.

A test account's wallet recharges, wallet transactions and bookings (and bookings by students paying from
its wallet) are never counted in revenue, and its recharge requests are never matched to the SRIC cash-book.
Reports carry ids, user types, counts and flags only (they are printed in public CI logs).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Iterable

from django.db.models import Count, Q, Sum

from iic_booking.users.test_accounts import TEST_EMAIL_DOMAIN

logger = logging.getLogger(__name__)

CONFIRM_TOKEN = "FLAG_TEST_ACCOUNTS"
_TEST_WORD = re.compile(r"(^|[^a-z])test([^a-z]|$)", re.IGNORECASE)


class TestAccountFlagError(ValueError):
    pass


def is_protected_account(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return bool(getattr(user, "is_superuser", False)) or getattr(user, "user_type", None) == UserType.ADMIN


def set_test_account_flag(user, value: bool, *, actor=None, source: str = "") -> bool:
    """Set ``User.is_test_account``; returns True when it changed. The Main Administrator account cannot be marked."""
    from iic_booking.users.models import User

    value = bool(value)
    if value and is_protected_account(user):
        raise TestAccountFlagError("The Main Administrator account cannot be marked as a test account.")
    if bool(user.is_test_account) == value:
        return False
    User.objects.filter(pk=user.pk).update(is_test_account=value)
    user.is_test_account = value
    logger.info(
        "is_test_account=%s for user %s by %s (%s)",
        value,
        user.pk,
        getattr(actor, "pk", None) or "system",
        source or "-",
    )
    return True


def candidate_reasons(user, *, linked_to_test_wallet: bool = False) -> list[str]:
    """Why an unflagged account looks like a test account (empty list: it does not)."""
    reasons = []
    email = (user.email or "").strip().lower()
    local, _, domain = email.partition("@")
    if domain == TEST_EMAIL_DOMAIN:
        reasons.append("test_email_domain")
    if (user.emp_id or "").strip().upper().startswith("TEST"):
        reasons.append("test_employee_id")
    if _TEST_WORD.search(user.name or ""):
        reasons.append("name_has_test")
    if _TEST_WORD.search(local.replace(".", " ").replace("_", " ")):
        reasons.append("email_has_test")
    if linked_to_test_wallet:
        reasons.append("linked_to_test_wallet")
    return reasons


def _money(value) -> str:
    return f"{Decimal(value or 0):.2f}"


def _activity(user_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    from iic_booking.equipment.models import Booking
    from iic_booking.users.models.wallet import SubWalletTransaction, WalletRechargeRequest

    ids = list(user_ids)
    out: dict[int, dict[str, Any]] = {i: {} for i in ids}
    for row in (
        WalletRechargeRequest.objects.filter(user_id__in=ids, is_deleted=False)
        .order_by()
        .values("user_id", "status")
        .annotate(n=Count("pk"), amount=Sum("amount"))
    ):
        out[row["user_id"]].setdefault("recharge_requests", {})[row["status"]] = f"{row['n']}/{_money(row['amount'])}"
    for row in (
        SubWalletTransaction.objects.filter(sub_wallet__wallet__user_id__in=ids)
        .order_by()
        .values("sub_wallet__wallet__user_id")
        .annotate(n=Count("pk"))
    ):
        out[row["sub_wallet__wallet__user_id"]]["wallet_transactions"] = row["n"]
    for row in Booking.objects.filter(user_id__in=ids).order_by().values("user_id").annotate(n=Count("pk")):
        out[row["user_id"]]["bookings"] = row["n"]
    return out


def build_report() -> dict[str, Any]:
    """Read-only: flagged accounts, unflagged accounts that look like test accounts, and the current SRIC follow-up list."""
    from iic_booking.users.models import User
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus, WalletRechargeRequest
    from iic_booking.users.wallet_recharge_workflow import overdue_fund_receipt_requests

    flagged = list(User.objects.filter(is_test_account=True).order_by("pk"))
    flagged_ids = [u.pk for u in flagged]
    linked = set(
        WalletJoinRequest.objects.filter(
            status=WalletJoinRequestStatus.APPROVED, wallet__user__is_test_account=True
        ).values_list("student_id", flat=True)
    )
    name_q = Q(name__iregex=r"(^|[^a-z])test([^a-z]|$)") | Q(email__icontains="test")
    pool = (
        User.objects.filter(is_test_account=False)
        .filter(
            name_q
            | Q(email__iendswith="@" + TEST_EMAIL_DOMAIN)
            | Q(emp_id__istartswith="TEST")
            | Q(pk__in=linked)
        )
        .order_by("pk")
    )
    candidates = []
    for u in pool:
        reasons = candidate_reasons(u, linked_to_test_wallet=u.pk in linked)
        if reasons:
            candidates.append((u, reasons))
    activity = _activity(flagged_ids + [u.pk for u, _ in candidates])

    def row(u, reasons=None):
        data = {
            "user_id": u.pk,
            "user_type": u.user_type,
            "is_active": bool(u.is_active),
            "has_wallet": hasattr(u, "wallet"),
            **activity.get(u.pk, {}),
        }
        if reasons is not None:
            data["reasons"] = reasons
        return data

    def brief(r):
        return {"request_id": r.pk, "user_id": r.user_id, "status": r.status, "amount": _money(r.amount)}

    days, overdue = overdue_fund_receipt_requests(WalletRechargeRequest.objects.all())
    shown = {r.pk for r in overdue}
    _, everything = overdue_fund_receipt_requests(WalletRechargeRequest.objects.all(), days, include_test=True)
    return {
        "flagged": [row(u) for u in flagged],
        "candidates": [row(u, reasons) for u, reasons in candidates],
        "sric_follow_up_days": days,
        "sric_follow_up": [brief(r) for r in overdue],
        "sric_follow_up_hidden_test": [brief(r) for r in everything if r.pk not in shown],
    }


def resolve_targets(user_ids: Iterable[str] = (), emails: Iterable[str] = ()) -> tuple[list, list[str]]:
    """Users for the given ids / emails, and the inputs that matched no user."""
    from iic_booking.users.models import User

    found: dict[int, Any] = {}
    missing: list[str] = []
    for raw in user_ids:
        value = str(raw).strip()
        if not value:
            continue
        user = User.objects.filter(pk=int(value)).first() if value.isdigit() else None
        if user is None:
            missing.append(f"id:{value}")
        else:
            found[user.pk] = user
    for raw in emails:
        value = str(raw).strip()
        if not value:
            continue
        user = User.objects.filter(email__iexact=value).first()
        if user is None:
            missing.append("email:<not found>")
        else:
            found[user.pk] = user
    return [found[k] for k in sorted(found)], missing
