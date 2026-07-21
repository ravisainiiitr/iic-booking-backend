"""
Faculty wallet-to-wallet (peer) transfer under a single department grant.

OTP is required; debit + credit run in one transaction with row locks.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWallet,
    WalletPeerTransfer,
    WalletPeerTransferStatus,
)
from iic_booking.users.repositories.wallet_repository import WalletRepository, SubWalletRepository
from iic_booking.users.wallet_recharge_ops import resolve_department_grant_code
from iic_booking.users.wallet_recharge_workflow import (
    find_department_account_incharges,
    find_department_administrators,
)

logger = logging.getLogger(__name__)


class PeerTransferError(Exception):
    """Validation / business rule failure for peer transfer."""

    def __init__(self, message: str, code: str = "invalid"):
        self.message = message
        self.code = code
        super().__init__(message)


def _as_money(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PeerTransferError("Invalid transfer amount.", "invalid_amount") from exc
    if amount <= 0:
        raise PeerTransferError("Transfer amount must be greater than zero.", "invalid_amount")
    if amount > Decimal("100000.00"):
        raise PeerTransferError("Maximum transfer amount is ₹1,00,000.", "amount_too_large")
    return amount


def assert_faculty_initiator(user) -> None:
    if getattr(user, "user_type", None) != UserType.FACULTY:
        raise PeerTransferError(
            "Only Faculty users can initiate wallet-to-wallet transfers.",
            "forbidden",
        )


def assert_eligible_recipient(sender, recipient) -> None:
    if recipient is None:
        raise PeerTransferError("Recipient not found.", "recipient_not_found")
    if not recipient.is_active:
        raise PeerTransferError("Recipient account is not active.", "recipient_inactive")
    if recipient.pk == sender.pk:
        raise PeerTransferError("You cannot transfer to your own wallet.", "transfer_to_self")
    if not recipient.can_have_wallet():
        raise PeerTransferError(
            "Recipient is not eligible for an individual wallet.",
            "recipient_ineligible",
        )
    # Internal peer transfers: faculty (and other own-wallet internal types) only
    if getattr(recipient, "user_type", None) not in {
        UserType.FACULTY,
        UserType.INDIVIDUAL_STUDENT,
    }:
        # Still allow if they own an internal-dept wallet-eligible account
        if not recipient.can_have_wallet():
            raise PeerTransferError("Recipient is not an eligible internal user.", "recipient_ineligible")


def assert_same_grant(department, recipient) -> str:
    """Ensure transfer department grant matches recipient eligibility under the same grant."""
    grant = resolve_department_grant_code(department)
    if not grant:
        raise PeerTransferError(
            "Selected department has no grant code configured.",
            "missing_grant",
        )
    # Recipient home department grant (if any) must match when they have a home dept
    home = getattr(recipient, "department", None)
    if home is not None:
        home_grant = resolve_department_grant_code(home)
        if home_grant and home_grant != grant:
            # Allow if recipient already holds a sub-wallet for this exact department
            has_same_dept_sw = SubWallet.objects.filter(
                wallet__user=recipient,
                department_id=department.id,
            ).exists()
            if not has_same_dept_sw:
                raise PeerTransferError(
                    f"Transfers between different grant codes are not allowed. "
                    f"Selected grant is {grant}; recipient's department grant is {home_grant}.",
                    "different_grant",
                )
    return grant


def list_eligible_recipients(*, sender, department, query: str = "", limit: int = 20) -> list[dict]:
    """Faculty/individual-student wallet owners eligible under the same grant as department."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    User = get_user_model()
    grant = resolve_department_grant_code(department)
    qs = (
        User.objects.filter(
            is_active=True,
            user_type__in=[UserType.FACULTY, UserType.INDIVIDUAL_STUDENT],
        )
        .exclude(pk=sender.pk)
        .select_related("department")
        .order_by("name", "email")
    )
    q = (query or "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(email__icontains=q)
            | Q(emp_id__icontains=q)
        )

    results = []
    for user in qs[: max(limit * 3, 30)]:
        if not user.can_have_wallet():
            continue
        try:
            assert_same_grant(department, user)
        except PeerTransferError:
            continue
        results.append(
            {
                "id": user.id,
                "name": user.name or user.email,
                "email": user.email,
                "emp_id": user.emp_id or "",
                "department": user.department.name if user.department_id else "",
                "has_wallet": hasattr(user, "wallet") and user.wallet is not None,
                "grant_code": grant,
            }
        )
        if len(results) >= limit:
            break
    return results


def create_pending_transfer(
    *,
    sender,
    recipient,
    department,
    amount,
    remarks: str = "",
) -> tuple[WalletPeerTransfer, str]:
    """Create PENDING_OTP transfer and generate OTP. Returns (transfer, otp)."""
    assert_faculty_initiator(sender)
    assert_eligible_recipient(sender, recipient)
    amount_dec = _as_money(amount)
    grant = assert_same_grant(department, recipient)

    sender_wallet = sender.get_accessible_wallet()
    if not sender_wallet or sender_wallet.user_id != sender.id:
        raise PeerTransferError("You must use your own faculty wallet to transfer.", "no_wallet")

    try:
        sender_sw = SubWallet.objects.get(wallet=sender_wallet, department=department)
    except SubWallet.DoesNotExist as exc:
        raise PeerTransferError(
            "No sub-wallet found for the selected department.",
            "no_sub_wallet",
        ) from exc

    if sender_sw.balance < amount_dec:
        raise PeerTransferError(
            f"Insufficient wallet balance. Available: ₹{sender_sw.balance}.",
            "insufficient_balance",
        )

    # Cancel older unused OTP drafts for same pair to reduce clutter
    WalletPeerTransfer.objects.filter(
        sender=sender,
        recipient=recipient,
        department=department,
        status=WalletPeerTransferStatus.PENDING_OTP,
    ).update(status=WalletPeerTransferStatus.CANCELLED, updated_at=timezone.now())

    transfer = WalletPeerTransfer.objects.create(
        sender=sender,
        recipient=recipient,
        initiated_by=sender,
        department=department,
        grant_code=grant,
        amount=amount_dec,
        remarks=(remarks or "").strip()[:2000],
        status=WalletPeerTransferStatus.PENDING_OTP,
    )
    otp = transfer.generate_otp()
    return transfer, otp


def send_transfer_otp_email(transfer: WalletPeerTransfer, otp: str) -> None:
    subject = f"OTP for Wallet Transfer {transfer.transaction_id} — ₹{transfer.amount}"
    body = f"""Your one-time password for wallet-to-wallet transfer:

OTP: {otp}

Transaction ID: {transfer.transaction_id}
Amount: ₹{transfer.amount}
Recipient: {transfer.recipient.name or transfer.recipient.email}
Department / Grant: {transfer.department.name} ({transfer.grant_code or '—'})
Expires in 10 minutes.

If you did not request this transfer, ignore this email.
""".strip()
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[transfer.sender.email],
        fail_silently=False,
    )


@transaction.atomic
def confirm_transfer_with_otp(*, transfer_id: int, sender, otp: str) -> WalletPeerTransfer:
    """Verify OTP and execute debit/credit atomically. Idempotent if already completed by sender."""
    locked = (
        WalletPeerTransfer.objects.select_for_update()
        .select_related("sender", "recipient", "department", "initiated_by")
        .filter(pk=transfer_id, sender=sender)
        .first()
    )
    if locked is None:
        raise PeerTransferError("Transfer request not found.", "not_found")

    if locked.status == WalletPeerTransferStatus.COMPLETED:
        return locked

    if locked.status != WalletPeerTransferStatus.PENDING_OTP:
        raise PeerTransferError(
            f"This transfer cannot be completed (status: {locked.get_status_display()}).",
            "invalid_status",
        )

    if locked.otp_expires_at and timezone.now() > locked.otp_expires_at:
        locked.status = WalletPeerTransferStatus.EXPIRED
        locked.failure_reason = "OTP expired"
        locked.save(update_fields=["status", "failure_reason", "updated_at"])
        raise PeerTransferError("OTP has expired. Please request a new OTP.", "otp_expired")

    if not locked.verify_otp(otp):
        raise PeerTransferError("Invalid or expired OTP.", "otp_invalid")

    # Re-validate eligibility under lock
    assert_faculty_initiator(sender)
    assert_eligible_recipient(sender, locked.recipient)
    assert_same_grant(locked.department, locked.recipient)

    sender_wallet = sender.get_accessible_wallet()
    if not sender_wallet or sender_wallet.user_id != sender.id:
        raise PeerTransferError("You must use your own faculty wallet to transfer.", "no_wallet")

    recipient_wallet = locked.recipient.get_accessible_wallet()
    if not recipient_wallet or recipient_wallet.user_id != locked.recipient_id:
        if locked.recipient.can_have_wallet():
            recipient_wallet, _ = WalletRepository.get_or_create(locked.recipient)
        else:
            raise PeerTransferError("Recipient does not have a wallet.", "recipient_no_wallet")

    sender_sw = (
        SubWallet.objects.select_for_update()
        .filter(wallet=sender_wallet, department_id=locked.department_id)
        .first()
    )
    if sender_sw is None:
        raise PeerTransferError("Sender sub-wallet not found.", "no_sub_wallet")

    # Lock recipient row if exists, else create then lock
    recipient_sw = (
        SubWallet.objects.select_for_update()
        .filter(wallet=recipient_wallet, department_id=locked.department_id)
        .first()
    )
    if recipient_sw is None:
        recipient_sw = SubWalletRepository.get_or_create(recipient_wallet, locked.department)
        recipient_sw = (
            SubWallet.objects.select_for_update().filter(pk=recipient_sw.pk).first()
        )

    # Re-acquire both locks in PK order to avoid deadlocks under concurrent transfers
    ordered_ids = sorted({sender_sw.pk, recipient_sw.pk})
    locked_map = {
        sw.pk: sw
        for sw in SubWallet.objects.select_for_update().filter(pk__in=ordered_ids)
    }
    sender_sw = locked_map[sender_sw.pk]
    recipient_sw = locked_map[recipient_sw.pk]

    desc_out = (
        f"Wallet transfer to {locked.recipient.name or locked.recipient.email} "
        f"[{locked.transaction_id}]"
    )
    desc_in = (
        f"Wallet transfer from {locked.sender.name or locked.sender.email} "
        f"[{locked.transaction_id}]"
    )
    if locked.remarks:
        desc_out += f" — {locked.remarks[:120]}"
        desc_in += f" — {locked.remarks[:120]}"

    try:
        sender_sw.debit(locked.amount, description=desc_out, related_user=sender)
        recipient_sw.credit(locked.amount, description=desc_in, related_user=locked.recipient)
    except ValueError as exc:
        locked.status = WalletPeerTransferStatus.FAILED
        locked.failure_reason = str(exc)
        locked.save(update_fields=["status", "failure_reason", "updated_at"])
        if "Insufficient" in str(exc):
            raise PeerTransferError(
                f"Insufficient wallet balance. Available: ₹{sender_sw.balance}.",
                "insufficient_balance",
            ) from exc
        raise PeerTransferError(str(exc), "transfer_failed") from exc

    sender_sw.refresh_from_db()
    recipient_sw.refresh_from_db()

    locked.otp_verified = True
    locked.otp_verified_at = timezone.now()
    locked.status = WalletPeerTransferStatus.COMPLETED
    locked.completed_at = timezone.now()
    locked.sender_balance_after = sender_sw.balance
    locked.recipient_balance_after = recipient_sw.balance
    locked.otp_code = ""  # clear secret
    locked.save()

    return locked


def notify_peer_transfer_completed(transfer: WalletPeerTransfer) -> None:
    """Email sender, recipient, department admin(s), and account in-charge(s)."""
    when = (transfer.completed_at or timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
    subject = (
        f"Wallet Transfer {transfer.transaction_id}: "
        f"₹{transfer.amount} — {transfer.sender.name or transfer.sender.email} → "
        f"{transfer.recipient.name or transfer.recipient.email}"
    )
    common = f"""Wallet-to-Wallet Transfer Completed

Transaction ID: {transfer.transaction_id}
Date and Time: {when}
Transfer Amount: ₹{transfer.amount}
Sender: {transfer.sender.name or transfer.sender.email} ({transfer.sender.email})
Recipient: {transfer.recipient.name or transfer.recipient.email} ({transfer.recipient.email})
Grant Code: {transfer.grant_code or '—'}
Department: {transfer.department.name if transfer.department_id else '—'}
Remarks: {transfer.remarks or '—'}
Initiated By: {transfer.initiated_by.email if transfer.initiated_by_id else '—'}
OTP Verification: Verified
Transaction Status: Completed
"""

    sender_body = common + f"\nYour updated sub-wallet balance: ₹{transfer.sender_balance_after}\n"
    recipient_body = common + f"\nYour updated sub-wallet balance: ₹{transfer.recipient_balance_after}\n"
    staff_body = common + "\nThis is an automated notification for department finance / administration.\n"

    pairs = [
        (transfer.sender.email, sender_body),
        (transfer.recipient.email, recipient_body),
    ]
    for u in find_department_administrators(transfer.department):
        if u.email:
            pairs.append((u.email, staff_body))
    for u in find_department_account_incharges(transfer.department):
        if u.email:
            pairs.append((u.email, staff_body))

    seen = set()
    for email, body in pairs:
        key = (email or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email.strip()],
                fail_silently=True,
            )
        except Exception:
            logger.exception("Failed peer-transfer email to %s for %s", email, transfer.transaction_id)
