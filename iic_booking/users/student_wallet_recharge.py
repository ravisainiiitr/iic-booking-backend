"""Helpers for IITR Student wallet recharge gating.

An IITR Student whose accessible wallet is their supervisor's (shared) wallet may recharge every
department of that wallet that faculty can recharge. Which methods are open per department follows
Wallet Payment Modes exactly as for faculty, except Project Grant, which stays faculty-only.

``Department.enable_student_wallet_recharge`` and the global
``WalletStudentRechargeSettings.enable_iitr_student_wallet_recharge`` are kept in the database for
compatibility but no longer gate anything.

Individual Students (fellowship holders with a wallet of their own) were retired after the 31 Oct 2026
committee meeting: every internal student now books on the supervisor's wallet. An existing Individual Student
can spend what is left in their wallet but can no longer top it up themselves. Staff may still record money
that was already received (direct / manual receipt recharge); nothing here moves or changes a balance.
"""

from __future__ import annotations

from typing import Any, Optional

from django.db.models import QuerySet

from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet_student_recharge_settings import (
    WalletStudentRechargeSettings,
)


def get_student_recharge_settings() -> WalletStudentRechargeSettings:
    return WalletStudentRechargeSettings.get_singleton()


def iitr_student_recharge_enabled() -> bool:
    """Legacy global toggle (kept for the admin settings API); does not gate recharge."""
    return bool(get_student_recharge_settings().enable_iitr_student_wallet_recharge)


def is_iitr_student(user: Any) -> bool:
    if user is None:
        return False
    return str(getattr(user, "user_type", "") or "") == UserType.STUDENT


INDIVIDUAL_STUDENT_TOPUPS_CLOSED_MESSAGE = (
    "Individual student wallets can no longer be recharged. You can use the balance that is left; after that, "
    "book on your supervisor's wallet (ask your supervisor to add you as a student on the Wallet page)."
)


def is_individual_student(user: Any) -> bool:
    return user is not None and str(getattr(user, "user_type", "") or "") == UserType.INDIVIDUAL_STUDENT


def student_recharge_forbidden_message() -> str:
    return (
        "Wallet recharge for IITR Students is available only for the departments of your "
        "supervisor's wallet. Link your supervisor's wallet on the Wallet page first."
    )


def student_otp_offline_forbidden_message() -> str:
    return (
        "IITR Students may recharge only via Direct Cash Deposit / Bank Transfer "
        "(or online payment when it is open). "
        "Project-grant OTP flow is not available for IITR Students."
    )


def student_supervisor_wallet(user: Any):
    """The supervisor's (shared) wallet the student uses, or None when not linked to one."""
    try:
        wallet = user.get_accessible_wallet()
    except Exception:
        return None
    if wallet is None or wallet.user_id == getattr(user, "id", None):
        return None
    return wallet


def student_recharge_departments(user: Any) -> QuerySet:
    """Departments this IITR Student may recharge: the faculty recharge list of the supervisor's wallet."""
    from iic_booking.users.models import Department
    from iic_booking.users.repositories.wallet_repository import (
        get_departments_for_wallet_recharge,
    )

    wallet = student_supervisor_wallet(user)
    if wallet is None:
        return Department.objects.none()
    return (
        get_departments_for_wallet_recharge(wallet)
        .exclude(name__iexact="ADMIN")
        .exclude(code__iexact="ADMIN")
    )


def assert_iitr_student_may_recharge(
    user: Any,
    department: Any = None,
    *,
    department_id: Optional[int] = None,
) -> Optional[str]:
    """
    Return an error message if this IITR Student must not recharge; else None.
    Individual Students may no longer top up their own wallet. Other non-students always pass (None).
    """
    if is_individual_student(user):
        return INDIVIDUAL_STUDENT_TOPUPS_CLOSED_MESSAGE
    if not is_iitr_student(user):
        return None

    dept_pk = getattr(department, "pk", None) if department is not None else department_id
    allowed = student_recharge_departments(user)
    if dept_pk is None:
        return None if allowed.exists() else student_recharge_forbidden_message()
    try:
        dept_pk = int(dept_pk)
    except (TypeError, ValueError):
        return student_recharge_forbidden_message()
    if allowed.filter(pk=dept_pk).exists():
        return None
    return student_recharge_forbidden_message()


def student_has_any_recharge_department(user: Any) -> bool:
    """True if the user can recharge at least one department sub-wallet (non-students: always)."""
    if is_individual_student(user):
        return False
    if not is_iitr_student(user):
        return True
    return student_recharge_departments(user).exists()
