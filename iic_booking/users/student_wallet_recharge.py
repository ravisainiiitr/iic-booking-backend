"""Helpers for IITR Student wallet recharge gating.

Temporary test allowlist: when non-empty, ONLY those emails may use
IITR Student wallet recharge (department flags are ignored for others).
"""

from __future__ import annotations

from typing import Any, Optional

from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet_student_recharge_settings import (
    WalletStudentRechargeSettings,
)

# Temporary: only these IITR Student accounts may use wallet recharge requests.
# Empty frozenset = fall back to department-wise enable_student_wallet_recharge.
STUDENT_WALLET_RECHARGE_EMAIL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "test.student@iic-booking.test",
    }
)


def get_student_recharge_settings() -> WalletStudentRechargeSettings:
    return WalletStudentRechargeSettings.get_singleton()


def iitr_student_recharge_enabled() -> bool:
    """Legacy global toggle (kept for admin settings page / API compat)."""
    return bool(get_student_recharge_settings().enable_iitr_student_wallet_recharge)


def is_iitr_student(user: Any) -> bool:
    if user is None:
        return False
    return str(getattr(user, "user_type", "") or "") == UserType.STUDENT


def _user_email_normalized(user: Any) -> str:
    return (getattr(user, "email", None) or "").strip().lower()


def student_on_recharge_allowlist(user: Any) -> bool:
    """True when allowlist is active and this user's email is listed."""
    if not STUDENT_WALLET_RECHARGE_EMAIL_ALLOWLIST:
        return False
    return _user_email_normalized(user) in STUDENT_WALLET_RECHARGE_EMAIL_ALLOWLIST


def student_recharge_forbidden_message() -> str:
    if STUDENT_WALLET_RECHARGE_EMAIL_ALLOWLIST:
        return (
            "Wallet recharge for IITR Students is currently limited to designated "
            "test accounts. Contact the administrator if you need access."
        )
    return (
        "Wallet recharge for IITR Students is disabled for this department. "
        "Ask a main administrator to enable it under Departments "
        "(IITR student wallet recharge)."
    )


def student_otp_offline_forbidden_message() -> str:
    return (
        "IITR Students may only recharge via the department wallet-recharge request "
        "(amount + department -> email Accept/Reject) when that department is enabled. "
        "Project-grant OTP flow is not available for IITR Students."
    )


def department_allows_student_recharge(department: Any) -> bool:
    if department is None:
        return False
    return bool(getattr(department, "enable_student_wallet_recharge", False))


def assert_iitr_student_may_recharge(
    user: Any,
    department: Any = None,
    *,
    department_id: Optional[int] = None,
) -> Optional[str]:
    """
    Return an error message if this IITR Student must not recharge; else None.
    Non-students always pass (None).

    When STUDENT_WALLET_RECHARGE_EMAIL_ALLOWLIST is non-empty, only listed
    emails are allowed (department flags bypassed for those accounts).
    Otherwise gating is department-wise (enable_student_wallet_recharge).
    """
    if not is_iitr_student(user):
        return None

    if STUDENT_WALLET_RECHARGE_EMAIL_ALLOWLIST:
        if student_on_recharge_allowlist(user):
            return None
        return student_recharge_forbidden_message()

    dept = department
    if dept is None and department_id is not None:
        from iic_booking.users.models import Department

        dept = Department.objects.filter(pk=int(department_id)).first()

    if dept is not None:
        if department_allows_student_recharge(dept):
            return None
        return student_recharge_forbidden_message()

    if iitr_student_recharge_enabled():
        return None
    return student_recharge_forbidden_message()


def student_has_any_recharge_department(user: Any) -> bool:
    """True if the student can recharge at least one department sub-wallet."""
    if not is_iitr_student(user):
        return True

    from iic_booking.users.repositories.wallet_repository import (
        get_departments_for_wallet_recharge,
    )

    wallet = None
    try:
        wallet = user.get_accessible_wallet()
    except Exception:
        wallet = None

    if STUDENT_WALLET_RECHARGE_EMAIL_ALLOWLIST:
        if not student_on_recharge_allowlist(user):
            return False
        return get_departments_for_wallet_recharge(wallet).exists()

    return get_departments_for_wallet_recharge(wallet).filter(
        enable_student_wallet_recharge=True,
    ).exists()
