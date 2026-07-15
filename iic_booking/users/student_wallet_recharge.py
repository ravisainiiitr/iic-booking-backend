"""Helpers for IITR Student wallet recharge gating."""

from __future__ import annotations

from typing import Any, Optional

from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet_student_recharge_settings import (
    WalletStudentRechargeSettings,
)


def get_student_recharge_settings() -> WalletStudentRechargeSettings:
    return WalletStudentRechargeSettings.get_singleton()


def iitr_student_recharge_enabled() -> bool:
    return bool(get_student_recharge_settings().enable_iitr_student_wallet_recharge)


def is_iitr_student(user: Any) -> bool:
    if user is None:
        return False
    return str(getattr(user, "user_type", "") or "") == UserType.STUDENT


def student_recharge_forbidden_message() -> str:
    return (
        "Wallet recharge for IITR Students is disabled. "
        "Ask an administrator to enable it under Wallet student recharge settings."
    )


def student_otp_offline_forbidden_message() -> str:
    return (
        "IITR Students may only recharge via SBIePay or Offline Request with a payment receipt. "
        "The OTP / accounts offline flow is not available for IITR Students."
    )


def assert_iitr_student_may_recharge(user: Any) -> Optional[str]:
    """
    Return an error message if this IITR Student must not recharge; else None.
    Non-students always pass (None).
    """
    if not is_iitr_student(user):
        return None
    if not iitr_student_recharge_enabled():
        return student_recharge_forbidden_message()
    return None
