"""
Faculty wallet recharge temporary credit facility (overdraft until parse credits the request).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models.user_type import UserType
from .models.wallet import (
    SubWallet,
    WalletRechargeCreditFacilityStatus,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from .models.wallet_credit_facility_settings import WalletCreditFacilitySettings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def get_credit_settings() -> WalletCreditFacilitySettings:
    return WalletCreditFacilitySettings.get_singleton()


def _effective_balance_threshold_inr(cfg: WalletCreditFacilitySettings) -> Decimal:
    """Threshold used for offers; invalid/zero/negative admin values fall back to ₹1000."""
    raw = Decimal(str(cfg.balance_threshold_inr))
    if raw <= 0:
        return Decimal("1000")
    return raw


def _active_credit_request_for_subwallet(sub: SubWallet) -> Optional[WalletRechargeRequest]:
    now = timezone.now()
    return (
        WalletRechargeRequest.objects.filter(
            wallet=sub.wallet,
            department=sub.department,
            status=WalletRechargeRequestStatus.PENDING,
            user_otp_verified=True,
            credit_facility_opted_in=True,
            credit_facility_status=WalletRechargeCreditFacilityStatus.ACTIVE,
            credit_window_ends_at__gt=now,
            credit_limit_amount__isnull=False,
        )
        .order_by("-credit_window_ends_at", "-id")
        .first()
    )


def subwallet_has_expired_credit_block(sub: SubWallet) -> bool:
    """True if this department sub-wallet is under booking hold (window ended, parse still pending)."""
    return WalletRechargeRequest.objects.filter(
        wallet=sub.wallet,
        department=sub.department,
        status=WalletRechargeRequestStatus.PENDING,
        credit_facility_status=WalletRechargeCreditFacilityStatus.EXPIRED_UNPAID,
    ).exists()


def wallet_booking_block_message(sub: SubWallet) -> Optional[str]:
    if not subwallet_has_expired_credit_block(sub):
        return None
    return (
        "Bookings for this department are on hold: your wallet recharge credit window ended before "
        "the recharge was credited via accounts (parse). Please complete the recharge process; "
        "bookings resume once the balance is restored."
    )


def subwallet_minimum_balance_after_debit(sub: SubWallet) -> Decimal:
    """
    Lowest allowed balance after a debit. Negative means overdraft allowed up to |value|.
    """
    if subwallet_has_expired_credit_block(sub):
        return Decimal("0.00")
    req = _active_credit_request_for_subwallet(sub)
    if not req or not req.credit_limit_amount:
        return Decimal("0.00")
    return -Decimal(str(req.credit_limit_amount))


def wallet_max_spendable_on_subwallet(sub: SubWallet) -> Decimal:
    """Maximum booking charge allowed right now (balance minus minimum allowed after full spend)."""
    sub.refresh_from_db()
    floor = subwallet_minimum_balance_after_debit(sub)
    return (sub.balance - floor).quantize(Decimal("0.01"))


def subwallet_booking_balance_ok(
    sub: SubWallet, total_charge, create_as_hold: bool
) -> tuple[bool, Optional[str]]:
    """Returns (ok, error_message). Used by booking flows and waitlist (avoid importing api_views)."""
    tc = Decimal(str(total_charge))
    if create_as_hold or tc <= 0:
        return True, None
    block = wallet_booking_block_message(sub)
    if block:
        return False, block
    spendable = wallet_max_spendable_on_subwallet(sub)
    if tc > spendable:
        return False, (
            f"Insufficient wallet balance. Required: ₹{tc:.2f}, Available: ₹{spendable:.2f}"
        )
    return True, None


def faculty_subwallet_balance_below_threshold(sub: SubWallet) -> bool:
    cfg = get_credit_settings()
    thr = _effective_balance_threshold_inr(cfg)
    sub.refresh_from_db()
    return sub.balance < thr


def another_active_credit_exists(wallet_id: int, department_id: int, exclude_request_id: Optional[int] = None) -> bool:
    qs = WalletRechargeRequest.objects.filter(
        wallet_id=wallet_id,
        department_id=department_id,
        status=WalletRechargeRequestStatus.PENDING,
        user_otp_verified=True,
        credit_facility_opted_in=True,
        credit_facility_status=WalletRechargeCreditFacilityStatus.ACTIVE,
        credit_window_ends_at__gt=timezone.now(),
    )
    if exclude_request_id is not None:
        qs = qs.exclude(pk=exclude_request_id)
    return qs.exists()


def suppress_credit_facility_offer(sub_wallet: SubWallet) -> bool:
    """
    When True, send-otp will not keep credit_facility_opted_in (facility not offered on that request).

    Popup / preflight still runs whenever balance is below threshold (except overdraft); only negative
    sub-wallet balance suppresses opting in on send-otp. Duplicate active credit lines are blocked
    in validate_credit_opt_in_for_send_otp; preflight exposes can_activate_new_credit for the UI.
    """
    sub_wallet.refresh_from_db()
    return sub_wallet.balance < Decimal("0")


def department_ids_pending_credit_opt_in(wallet) -> list[int]:
    """Department IDs with a pending recharge that already uses the credit-facility path (any OTP stage)."""
    qs = (
        WalletRechargeRequest.objects.filter(
            wallet=wallet,
            status=WalletRechargeRequestStatus.PENDING,
            credit_facility_opted_in=True,
            department_id__isnull=False,
        )
        .values_list("department_id", flat=True)
        .distinct()
    )
    return list(qs)


def credit_facility_offer_for_recharge_preflight(user, department_id: int) -> Dict[str, Any]:
    """
    Faculty: whether the wallet UI should show the credit-facility popup before calling send-otp.

    show_offer is True whenever the department sub-wallet balance is below the admin threshold and
    not in overdraft — including when a temporary credit line is already active (faculty still sees
    the dialog; can_activate_new_credit is False in that case so the UI can skip "Accept credit").
    """
    cfg = get_credit_settings()
    thr_eff = _effective_balance_threshold_inr(cfg)
    result: Dict[str, Any] = {
        "show_offer": False,
        "can_activate_new_credit": False,
        "balance_threshold_inr": str(thr_eff),
        "credit_window_days": int(cfg.credit_window_days),
        "max_credit_inr": str(cfg.max_credit_inr),
        "sub_wallet_balance": "0.00",
        "reason": "not_faculty",
    }
    if not user.is_faculty():
        return result

    from .repositories.wallet_repository import (
        WalletRepository,
        SubWalletRepository,
        resolve_internal_department_for_wallet_recharge,
    )

    result["reason"] = "no_wallet"
    wallet = user.get_accessible_wallet()
    if not wallet:
        if user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(user)
        if not wallet:
            return result

    result["reason"] = "invalid_department"
    department = resolve_internal_department_for_wallet_recharge(wallet, department_id)
    if not department:
        return result

    sub = SubWalletRepository.get_or_create(wallet, department)
    sub.refresh_from_db()
    result["sub_wallet_balance"] = str(sub.balance.quantize(Decimal("0.01")))

    if sub.balance < Decimal("0"):
        result["reason"] = "negative_balance"
        return result

    if not faculty_subwallet_balance_below_threshold(sub):
        result["reason"] = "at_or_above_threshold"
        return result

    result["show_offer"] = True
    result["can_activate_new_credit"] = not another_active_credit_exists(wallet.id, department.id)
    result["reason"] = "below_threshold"
    return result


def validate_credit_opt_in_for_send_otp(
    *,
    user,
    wallet,
    department,
    credit_facility_opted_in: bool,
    sub_wallet: SubWallet,
) -> Optional[str]:
    """Return error message if opt-in is invalid, else None."""
    if not credit_facility_opted_in:
        return None
    if not user.is_faculty():
        return "Credit facility is only available for faculty wallet recharge requests."
    if another_active_credit_exists(wallet.id, department.id):
        return "You already have an active credit facility for this department. Complete or wait for that recharge request first."
    if not faculty_subwallet_balance_below_threshold(sub_wallet):
        thr = _effective_balance_threshold_inr(get_credit_settings())
        return (
            f"Credit facility is offered only when the department balance is below "
            f"₹{thr}."
        )
    return None


def try_activate_credit_facility_after_otp_verify(recharge_request: WalletRechargeRequest) -> None:
    """
    After user OTP is verified: if opted in and rules still pass, set ACTIVE credit window and limit.
    Otherwise clear opt-in flag so the request proceeds as a normal recharge request.
    """
    if not recharge_request.credit_facility_opted_in:
        return
    if recharge_request.user.user_type != UserType.FACULTY:
        WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(credit_facility_opted_in=False)
        return
    if not recharge_request.department_id:
        WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(credit_facility_opted_in=False)
        return

    sub, _ = SubWallet.objects.get_or_create(
        wallet=recharge_request.wallet,
        department=recharge_request.department,
        defaults={"balance": Decimal("0.00")},
    )
    sub.refresh_from_db()
    if sub.balance < Decimal("0"):
        WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(credit_facility_opted_in=False)
        return
    if another_active_credit_exists(
        recharge_request.wallet_id,
        recharge_request.department_id,
        exclude_request_id=recharge_request.id,
    ):
        WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(credit_facility_opted_in=False)
        return
    if not faculty_subwallet_balance_below_threshold(sub):
        WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(credit_facility_opted_in=False)
        return

    cfg = get_credit_settings()
    cap = min(Decimal(str(cfg.max_credit_inr)), Decimal(str(recharge_request.amount)))
    ends = timezone.now() + timedelta(days=int(cfg.credit_window_days))
    WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(
        credit_limit_amount=cap,
        credit_window_ends_at=ends,
        credit_facility_status=WalletRechargeCreditFacilityStatus.ACTIVE,
    )
    recharge_request.refresh_from_db()
    try:
        from iic_booking.communication.wallet_notifications import (
            send_wallet_credit_facility_activated_user_email,
        )

        send_wallet_credit_facility_activated_user_email(recharge_request)
    except Exception as ex:
        logger.warning(
            "Credit facility activation email failed for recharge request %s: %s",
            recharge_request.pk,
            ex,
            exc_info=True,
        )


def expire_due_wallet_credit_facilities() -> int:
    """
    Mark ACTIVE credit windows as EXPIRED_UNPAID when past end time and request still pending.
    Sends one email per request. Returns number of requests expired in this run.
    """
    now = timezone.now()
    qs = WalletRechargeRequest.objects.filter(
        status=WalletRechargeRequestStatus.PENDING,
        credit_facility_status=WalletRechargeCreditFacilityStatus.ACTIVE,
        credit_window_ends_at__lte=now,
        credit_facility_opted_in=True,
    ).select_related("user", "department")
    count = 0
    for req in qs:
        with transaction.atomic():
            locked = (
                WalletRechargeRequest.objects.select_for_update()
                .filter(
                    pk=req.pk,
                    status=WalletRechargeRequestStatus.PENDING,
                    credit_facility_status=WalletRechargeCreditFacilityStatus.ACTIVE,
                )
                .first()
            )
            if not locked:
                continue
            locked.credit_facility_status = WalletRechargeCreditFacilityStatus.EXPIRED_UNPAID
            update_fields = ["credit_facility_status", "updated_at"]
            if locked.credit_expiry_notified_at is None:
                _send_credit_expired_hold_email(locked)
                locked.credit_expiry_notified_at = timezone.now()
                update_fields.append("credit_expiry_notified_at")
            locked.save(update_fields=update_fields)
            count += 1
    return count


def _send_credit_expired_hold_email(req: WalletRechargeRequest) -> None:
    user = req.user
    if not user or not user.email:
        return
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.utils import get_frontend_absolute_url

    dept = req.department.name if req.department else "your department"
    link = get_frontend_absolute_url("/wallet")
    context = {
        "user_name": user.name or user.email,
        "user_email": user.email,
        "request_id": str(req.id),
        "department_name": dept,
        "amount": f"{req.amount:.2f}",
        "link": link,
    }
    try:
        CommunicationService.send_email(
            recipient=user,
            template="wallet_credit_facility_expired_email",
            template_context=context,
            metadata={
                "notification_type": "warning",
                "wallet_recharge_request_id": req.id,
            },
            created_by=None,
        )
        return
    except Exception as e:
        logger.warning(
            "Template email wallet_credit_facility_expired_email failed for request %s (%s); falling back to send_mail",
            req.id,
            e,
        )
    subject = "[IIC] Wallet recharge credit window ended — bookings on hold"
    body = (
        f"Dear {user.name or user.email},\n\n"
        f"The temporary credit facility linked to wallet recharge request #{req.id} ({dept}) has ended "
        f"because the recharge was not credited via the accounts parse process within the allowed window.\n\n"
        f"Further equipment bookings against that department wallet are on hold until the recharge is "
        f"realized and your balance is no longer in default.\n\n"
        f"If the recharge is credited later, your wallet will be updated and bookings can resume when the balance allows.\n\n"
        f"Wallet: {link}\n\n"
        f"— IIC Booking System\n"
    )
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("credit expiry fallback email failed for request %s: %s", req.id, e)


def serialize_faculty_credit_status_for_wallet(wallet, *, for_user) -> list[dict[str, Any]]:
    """Rows for dashboard / login timeline (wallet owner's pending credit lines)."""
    if not wallet:
        return []
    # Only wallet owner (faculty) or any user on shared wallet sees alerts for that wallet
    qs = (
        WalletRechargeRequest.objects.filter(
            wallet=wallet,
            status=WalletRechargeRequestStatus.PENDING,
            credit_facility_opted_in=True,
        )
        .filter(
            credit_facility_status__in=[
                WalletRechargeCreditFacilityStatus.ACTIVE,
                WalletRechargeCreditFacilityStatus.EXPIRED_UNPAID,
            ]
        )
        .select_related("department")
        .order_by("-created_at")[:20]
    )
    out: list[dict[str, Any]] = []
    for r in qs:
        sub = SubWallet.objects.filter(wallet=wallet, department_id=r.department_id).first()
        bal = str(sub.balance) if sub else "0.00"
        out.append(
            {
                "request_id": r.id,
                "department_id": r.department_id,
                "department_name": r.department.name if r.department else "",
                "amount": str(r.amount),
                "credit_limit_inr": str(r.credit_limit_amount) if r.credit_limit_amount is not None else None,
                "credit_window_ends_at": r.credit_window_ends_at.isoformat() if r.credit_window_ends_at else None,
                "credit_facility_status": r.credit_facility_status,
                "sub_wallet_balance": bal,
                "bookings_blocked": r.credit_facility_status
                == WalletRechargeCreditFacilityStatus.EXPIRED_UNPAID,
            }
        )
    return out
