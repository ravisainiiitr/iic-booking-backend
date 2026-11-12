"""External users: wallet recharge against an offline payment, with a receipt, scoped approval and reconciliation.

Flow
----
1. The external user declares the payment (amount, date, mode, UTR, payer bank and account name, where it was paid
   into, remarks) and uploads the receipt. A ``WalletRechargeRequest`` (mode ``offline_payment``, PENDING) is created
   with an ``OfflineRechargePayment`` detail row and a ``DepartmentPaymentReceipt`` holding the file.
2. The Main Administrator (any department), or the Department Administrator / Accounts In Charge of the request's
   department, approves (with a note, optionally a lower amount) or rejects (with a reason).
3. Approval credits the sub-wallet once through ``wallet_recharge_workflow.approve_request`` (row lock + PENDING
   check) and links the single ``SubWalletTransaction``; the detail moves to Awaiting reconciliation.
4. The Main Administrator or the department's Accounts In Charge marks it Reconciled (bank / grant credit reference
   and date) or flags a Mismatch (note). A mismatch can later be reconciled.

Every step appends to ``WalletRechargeRequestAuditLog`` (append-only) and notifies the user by email and in-app.
Only external users with their own wallet may submit; while ``EXTERNAL_OFFLINE_RECHARGE_ALL_EXTERNAL_USERS`` is off
it is limited to test accounts.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from iic_booking.users.models.offline_recharge import (
    OfflinePaymentMode,
    OfflineRechargePayment,
    OfflineReconciliationStatus,
)
from iic_booking.users.models.payment import (
    DepartmentPaymentReceipt,
    DepartmentPaymentReceiptPurpose,
    DepartmentPaymentReceiptStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    WalletRechargeCancellationSource,
    WalletRechargeMode,
    WalletRechargeRejectionReason,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)

logger = logging.getLogger(__name__)

MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("10000000.00")
MAX_RECEIPT_BYTES = 10 * 1024 * 1024
RECEIPT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
NOTE_MAX = 2000
WALLET_LINK = "/wallet?tab=offline-payments"
APPROVER_LINK = "/admin-settings/offline-recharges"


class OfflineRechargeError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", field: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.field = field


# --- who may do what -------------------------------------------------------------------------------------------


def feature_open_for(user) -> bool:
    """External user covered by the rollout (all external users, or test accounts while it is being piloted)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not UserType.is_external_user(getattr(user, "user_type", None) or ""):
        return False
    if getattr(settings, "EXTERNAL_OFFLINE_RECHARGE_ALL_EXTERNAL_USERS", False):
        return True
    return bool(getattr(user, "is_test_account", False))


def _active(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and getattr(user, "is_active", False))


def approver_role(user) -> str:
    """'main_admin' | 'dept_admin' | 'accounts' | '' for a user who may see the approval queue."""
    if not _active(user):
        return ""
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return "main_admin"
    if ut == UserType.DEPT_ADMIN and getattr(user, "department_id", None):
        return "dept_admin"
    if ut == UserType.FINANCE and getattr(user, "department_id", None):
        return "accounts"
    return ""


def can_decide(user, req: WalletRechargeRequest) -> bool:
    """Approve / reject: Main Administrator anywhere; Department Administrator or Accounts In Charge in scope."""
    role = approver_role(user)
    if not role or req.user_id == getattr(user, "pk", None):
        return False
    if role == "main_admin":
        return True
    return bool(req.department_id and req.department_id == user.department_id)


def can_reconcile(user, req: WalletRechargeRequest) -> bool:
    """Reconciliation is accounts work: the Main Administrator or the department's Accounts In Charge."""
    role = approver_role(user)
    if role == "main_admin":
        return req.user_id != user.pk
    return role == "accounts" and bool(req.department_id and req.department_id == user.department_id)


def scoped_queryset(user):
    qs = (
        WalletRechargeRequest.objects.filter(recharge_mode=WalletRechargeMode.OFFLINE_PAYMENT, is_deleted=False)
        .select_related("user", "department", "offline_payment", "offline_payment__receipt", "processed_by")
        .order_by("-created_at")
    )
    role = approver_role(user)
    if role == "main_admin":
        return qs
    if role in ("dept_admin", "accounts"):
        return qs.filter(department_id=user.department_id)
    return qs.none()


# --- state shown to users and approvers ------------------------------------------------------------------------

STATE_LABELS = {
    "pending": "Pending approval",
    "approved": "Approved · awaiting reconciliation",
    "reconciled": "Reconciled",
    "mismatch": "Mismatch flagged",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
}


def state_of(req: WalletRechargeRequest) -> str:
    if req.status == WalletRechargeRequestStatus.PENDING:
        return "pending"
    if req.status == WalletRechargeRequestStatus.REJECTED:
        return "rejected"
    if req.status == WalletRechargeRequestStatus.CANCELLED:
        return "cancelled"
    detail = getattr(req, "offline_payment", None)
    rec = getattr(detail, "reconciliation_status", "")
    if rec == OfflineReconciliationStatus.RECONCILED:
        return "reconciled"
    if rec == OfflineReconciliationStatus.MISMATCH:
        return "mismatch"
    return "approved"


def filter_by_state(qs, state: str):
    state = (state or "").strip().lower()
    if state == "pending":
        return qs.filter(status=WalletRechargeRequestStatus.PENDING)
    if state == "rejected":
        return qs.filter(status=WalletRechargeRequestStatus.REJECTED)
    if state == "cancelled":
        return qs.filter(status=WalletRechargeRequestStatus.CANCELLED)
    if state == "approved":
        return qs.filter(
            status=WalletRechargeRequestStatus.APPROVED,
            offline_payment__reconciliation_status=OfflineReconciliationStatus.AWAITING,
        )
    if state == "reconciled":
        return qs.filter(offline_payment__reconciliation_status=OfflineReconciliationStatus.RECONCILED)
    if state == "mismatch":
        return qs.filter(offline_payment__reconciliation_status=OfflineReconciliationStatus.MISMATCH)
    return qs


def _file_url(receipt) -> Optional[str]:
    try:
        return receipt.receipt_file.url if receipt and receipt.receipt_file else None
    except Exception:  # noqa: BLE001 - storage misconfiguration must not break the list
        return None


def serialize(req: WalletRechargeRequest, *, viewer=None, include_audit: bool = False) -> dict[str, Any]:
    from iic_booking.users.display import get_user_display_name

    d: OfflineRechargePayment = req.offline_payment
    state = state_of(req)
    out: dict[str, Any] = {
        "id": req.pk,
        "reference": req.transaction_number,
        "state": state,
        "state_label": STATE_LABELS[state],
        "status": req.status,
        "department": {"id": req.department_id, "name": getattr(req.department, "name", "")},
        "claimed_amount": str(d.claimed_amount),
        "approved_amount": str(req.amount) if req.status == WalletRechargeRequestStatus.APPROVED else None,
        "payment_date": d.payment_date.isoformat(),
        "payment_mode": d.payment_mode,
        "payment_mode_label": d.get_payment_mode_display(),
        "payment_mode_other": d.payment_mode_other,
        "transaction_reference": d.transaction_reference,
        "payer_bank": d.payer_bank,
        "payer_account_name": d.payer_account_name,
        "paid_into": d.paid_into,
        "remarks": d.remarks,
        "receipt_url": _file_url(d.receipt),
        "receipt_name": os.path.basename(getattr(d.receipt.receipt_file, "name", "") or ""),
        "decision_note": req.response_message,
        "decided_at": req.responded_at.isoformat() if req.responded_at else None,
        "decided_by": get_user_display_name(req.processed_by) if req.processed_by_id else "",
        "wallet_transaction_id": d.wallet_transaction_id,
        "reconciliation_status": d.reconciliation_status,
        "reconciled_reference": d.reconciled_reference,
        "reconciled_credit_date": d.reconciled_credit_date.isoformat() if d.reconciled_credit_date else None,
        "reconciliation_note": d.reconciliation_note,
        "reconciled_at": d.reconciled_at.isoformat() if d.reconciled_at else None,
        "created_at": req.created_at.isoformat(),
        "user": {
            "id": req.user_id,
            "name": get_user_display_name(req.user),
            "email": req.user.email,
            "organisation": getattr(getattr(req.user, "department", None), "name", "") or "",
        },
    }
    if viewer is not None:
        out["can_decide"] = state == "pending" and can_decide(viewer, req)
        out["can_reconcile"] = state in ("approved", "mismatch") and can_reconcile(viewer, req)
        out["can_cancel"] = state == "pending" and req.user_id == viewer.pk
    if include_audit:
        out["audit"] = [
            {
                "action": log.action,
                "from_status": log.from_status,
                "to_status": log.to_status,
                "actor": get_user_display_name(log.actor) if log.actor_id else (log.actor_email or "system"),
                "message": log.message,
                "at": log.created_at.isoformat(),
            }
            for log in req.audit_logs.select_related("actor").order_by("created_at", "pk")
        ]
    return out


# --- input validation ------------------------------------------------------------------------------------------


def _text(data, key: str, *, required: bool, max_len: int, label: str) -> str:
    value = str(data.get(key) or "").strip()
    if required and not value:
        raise OfflineRechargeError(f"{label} is required.", field=key)
    if len(value) > max_len:
        raise OfflineRechargeError(f"{label} must be at most {max_len} characters.", field=key)
    return value


def parse_amount(raw, *, field: str = "amount") -> Decimal:
    try:
        amount = Decimal(str(raw).replace(",", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise OfflineRechargeError("Enter a valid amount.", field=field) from None
    if amount < MIN_AMOUNT or amount > MAX_AMOUNT:
        raise OfflineRechargeError(f"Amount must be between ₹{MIN_AMOUNT} and ₹{MAX_AMOUNT:,.0f}.", field=field)
    return amount


def parse_date(raw, *, field: str, label: str, not_future: bool = True) -> date:
    try:
        value = date.fromisoformat(str(raw or "").strip()[:10])
    except ValueError:
        raise OfflineRechargeError(f"{label} must be a date (YYYY-MM-DD).", field=field) from None
    if not_future and value > timezone.localdate():
        raise OfflineRechargeError(f"{label} cannot be in the future.", field=field)
    return value


def normalise_reference(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").upper()


def validate_receipt(upload) -> None:
    if upload is None:
        raise OfflineRechargeError("Upload the transaction receipt (image or PDF).", field="receipt_file")
    ext = os.path.splitext(getattr(upload, "name", "") or "")[1].lower()
    if ext not in RECEIPT_EXTENSIONS:
        raise OfflineRechargeError("The receipt must be a PDF or an image (JPG, PNG, WEBP, HEIC).", field="receipt_file")
    size = getattr(upload, "size", 0) or 0
    if size <= 0:
        raise OfflineRechargeError("The receipt file is empty.", field="receipt_file")
    if size > MAX_RECEIPT_BYTES:
        raise OfflineRechargeError("The receipt must be 10 MB or smaller.", field="receipt_file")
    head = upload.read(16)
    upload.seek(0)
    ok = (
        head.startswith(b"%PDF")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        or head[4:8] == b"ftyp"
    )
    if not ok:
        raise OfflineRechargeError("The receipt file does not look like a PDF or an image.", field="receipt_file")


@dataclass
class SubmitResult:
    request: WalletRechargeRequest
    created: bool


# --- notifications -----------------------------------------------------------------------------------------------


def _notify(key: str, recipients, *, title: str, message: str, link: str) -> None:
    try:
        from iic_booking.external_services.notifications import notify

        notify(key, recipients, title=title, message=message, link=link)
    except Exception:  # noqa: BLE001 - a notification never breaks the action
        logger.exception("offline recharge notification failed key=%s", key)


def _approvers_for(req: WalletRechargeRequest) -> list:
    """Main Administrators, and the Department Administrators and Accounts In Charge of the request's department.

    A test account's request is announced only to test-account approvers, and a real request only to real
    approvers, so trials on production never reach real staff (as with Direct Cash test requests)."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from iic_booking.users.test_accounts import is_test_user

    scope = Q(user_type=UserType.ADMIN)
    if req.department_id:
        scope |= Q(user_type__in=[UserType.DEPT_ADMIN, UserType.FINANCE], department_id=req.department_id)
    approvers = get_user_model().objects.filter(scope, is_active=True)
    test_request = is_test_user(req.user)
    return [u for u in approvers if is_test_user(u) == test_request]


def _money(value) -> str:
    return f"₹{Decimal(value):,.2f}"


# --- actions -----------------------------------------------------------------------------------------------------


def submit(user, data, upload) -> SubmitResult:
    from iic_booking.users.repositories.wallet_repository import (
        WalletRepository,
        resolve_internal_department_for_wallet_recharge,
    )
    from iic_booking.users.wallet_recharge_workflow import append_audit_log, populate_request_snapshots

    if not UserType.is_external_user(getattr(user, "user_type", None) or ""):
        raise OfflineRechargeError("Offline payment recharge is available to external users only.", status=403, code="not_external")
    if not feature_open_for(user):
        raise OfflineRechargeError("Offline payment recharge is not open for your account yet.", status=403, code="not_open")

    client_request_id = str(data.get("client_request_id") or "").strip()[:64]
    if client_request_id:
        existing = OfflineRechargePayment.objects.filter(client_request_id=client_request_id).select_related(
            "recharge_request"
        ).first()
        if existing is not None:
            if existing.recharge_request.user_id != user.pk:
                raise OfflineRechargeError("This request id is already in use.", status=409, code="duplicate_client_id")
            return SubmitResult(existing.recharge_request, False)

    amount = parse_amount(data.get("amount"))
    payment_date = parse_date(data.get("payment_date"), field="payment_date", label="Date of payment")
    mode = str(data.get("payment_mode") or "").strip().upper()
    if mode not in OfflinePaymentMode.values:
        raise OfflineRechargeError("Choose the payment mode.", field="payment_mode")
    mode_other = _text(data, "payment_mode_other", required=mode == OfflinePaymentMode.OTHER, max_len=60, label="Mode (other)")
    reference = normalise_reference(_text(data, "transaction_reference", required=True, max_len=64, label="UTR / transaction reference"))
    if len(reference) < 4:
        raise OfflineRechargeError("Enter the full UTR / transaction reference.", field="transaction_reference")
    payer_bank = _text(data, "payer_bank", required=True, max_len=120, label="Payer bank")
    payer_account_name = _text(data, "payer_account_name", required=True, max_len=160, label="Payer account name")
    paid_into = _text(data, "paid_into", required=False, max_len=160, label="Paid into")
    remarks = _text(data, "remarks", required=False, max_len=NOTE_MAX, label="Remarks")
    validate_receipt(upload)

    try:
        department_id = int(data.get("department_id"))
    except (TypeError, ValueError):
        raise OfflineRechargeError("Choose the department to credit.", field="department_id") from None

    wallet = user.get_accessible_wallet()
    if wallet is None and user.can_have_wallet():
        wallet, _ = WalletRepository.get_or_create(user)
    if wallet is None or wallet.user_id != user.pk:
        raise OfflineRechargeError("Your account has no wallet of its own.", status=403, code="no_wallet")
    department = resolve_internal_department_for_wallet_recharge(wallet, department_id)
    if department is None:
        raise OfflineRechargeError("Choose a department from the recharge list.", field="department_id")

    clash = OfflineRechargePayment.objects.filter(transaction_reference=reference).exclude(
        recharge_request__status__in=[WalletRechargeRequestStatus.REJECTED, WalletRechargeRequestStatus.CANCELLED]
    )
    if clash.exists():
        raise OfflineRechargeError(
            "A recharge with this UTR / transaction reference is already on file.",
            status=409,
            code="duplicate_reference",
            field="transaction_reference",
        )

    try:
        with transaction.atomic():
            req = WalletRechargeRequest.objects.create(
                user=user,
                wallet=wallet,
                department=department,
                amount=amount,
                recharge_mode=WalletRechargeMode.OFFLINE_PAYMENT,
                status=WalletRechargeRequestStatus.PENDING,
                # The signed-in user submits the payment with its receipt; there is no OTP step for this mode.
                user_otp_verified=True,
                utr_reference=reference,
            )
            populate_request_snapshots(req)
            receipt = DepartmentPaymentReceipt.objects.create(
                utr_reference=reference,
                department=department,
                user=user,
                amount=amount,
                purpose=DepartmentPaymentReceiptPurpose.WALLET_RECHARGE,
                wallet_recharge_request=req,
                payment_date=payment_date,
                receipt_file=upload,
            )
            OfflineRechargePayment.objects.create(
                recharge_request=req,
                client_request_id=client_request_id,
                claimed_amount=amount,
                payment_date=payment_date,
                payment_mode=mode,
                payment_mode_other=mode_other,
                transaction_reference=reference,
                payer_bank=payer_bank,
                payer_account_name=payer_account_name,
                paid_into=paid_into,
                remarks=remarks,
                receipt=receipt,
            )
            append_audit_log(
                req,
                action="offline_payment_submitted",
                to_status=WalletRechargeRequestStatus.PENDING,
                actor=user,
                message=f"Offline {mode} payment {_money(amount)} ref {reference} submitted with receipt.",
                metadata={"amount": str(amount), "payment_mode": mode, "payment_date": payment_date.isoformat()},
            )
            _notify(
                f"offline-recharge:{req.pk}:submitted",
                [user],
                title=f"Recharge request {req.transaction_number} received",
                message=(
                    f"Your offline payment of {_money(amount)} (ref {reference}) is waiting for approval by "
                    f"{department.name}. You will be told when it is approved or rejected."
                ),
                link=WALLET_LINK,
            )
            _notify(
                f"offline-recharge:{req.pk}:to-approvers",
                _approvers_for(req),
                title=f"Offline payment to approve: {req.transaction_number}",
                message=(
                    f"An external user declared an offline {mode} payment of {_money(amount)} (ref {reference}) "
                    f"for {department.name}. Check the receipt and approve or reject it."
                ),
                link=APPROVER_LINK,
            )
    except IntegrityError:
        raise OfflineRechargeError(
            "A recharge with this UTR / transaction reference is already on file.",
            status=409,
            code="duplicate_reference",
            field="transaction_reference",
        ) from None
    return SubmitResult(req, True)


def _lock(request_id: int) -> WalletRechargeRequest:
    try:
        return (
            WalletRechargeRequest.objects.select_for_update(of=("self",))
            .select_related("user", "department")
            .get(pk=request_id, recharge_mode=WalletRechargeMode.OFFLINE_PAYMENT, is_deleted=False)
        )
    except WalletRechargeRequest.DoesNotExist:
        raise OfflineRechargeError("Request not found.", status=404, code="not_found") from None


def _lock_detail(req: WalletRechargeRequest) -> OfflineRechargePayment:
    return OfflineRechargePayment.objects.select_for_update().get(recharge_request=req)


def _note(raw, *, label: str) -> str:
    note = str(raw or "").strip()
    if not note:
        raise OfflineRechargeError(f"{label} is required.", field="note")
    if len(note) > NOTE_MAX:
        raise OfflineRechargeError(f"{label} must be at most {NOTE_MAX} characters.", field="note")
    return note


def _released_reference(detail: OfflineRechargePayment) -> str:
    """Receipt key after a rejection or cancellation, so the same UTR can be submitted again (the detail row keeps
    the declared reference)."""
    suffix = f"~X{detail.recharge_request_id}"
    return detail.transaction_reference[: 64 - len(suffix)] + suffix


def _already(req) -> OfflineRechargeError:
    return OfflineRechargeError(
        f"This request is already {STATE_LABELS[state_of(req)].lower()}.", status=409, code="already_processed"
    )


def approve(request_id: int, actor, *, note, approved_amount=None) -> WalletRechargeRequest:
    from iic_booking.users.wallet_recharge_workflow import RechargeAlreadyProcessed, append_audit_log, approve_request

    note = _note(note, label="Approval note")
    with transaction.atomic():
        req = _lock(request_id)
        if not can_decide(actor, req):
            raise OfflineRechargeError("You cannot act on this request.", status=403, code="forbidden")
        detail = _lock_detail(req)
        if req.status != WalletRechargeRequestStatus.PENDING or detail.wallet_transaction_id:
            raise _already(req)
        amount = detail.claimed_amount
        if approved_amount not in (None, ""):
            amount = parse_amount(approved_amount, field="approved_amount")
            if amount > detail.claimed_amount:
                raise OfflineRechargeError("The approved amount cannot exceed the amount paid.", field="approved_amount")
        if amount != req.amount:
            req.amount = amount
            req.save(update_fields=["amount", "updated_at"])
        try:
            approved = approve_request(req, response_message=note, actor=actor)
        except RechargeAlreadyProcessed:
            raise _already(req) from None
        txn = getattr(approved, "_credit_transaction", None)
        if txn is None:  # never deferred for this mode; refuse rather than approve without a credit
            raise OfflineRechargeError("The wallet could not be credited.", status=500, code="no_credit")
        detail.wallet_transaction = txn
        detail.reconciliation_status = OfflineReconciliationStatus.AWAITING
        detail.save(update_fields=["wallet_transaction", "reconciliation_status", "updated_at"])
        DepartmentPaymentReceipt.objects.filter(pk=detail.receipt_id).update(
            status=DepartmentPaymentReceiptStatus.PROCESSED,
            amount=amount,
            finance_processed_at=timezone.now(),
            finance_processed_by=actor,
            finance_remarks=note[:NOTE_MAX],
        )
        if amount != detail.claimed_amount:
            append_audit_log(
                approved,
                action="offline_payment_amount_changed",
                from_status=approved.status,
                to_status=approved.status,
                actor=actor,
                message=f"Approved {_money(amount)} of the {_money(detail.claimed_amount)} declared.",
                metadata={"claimed_amount": str(detail.claimed_amount), "approved_amount": str(amount)},
            )
        _notify(
            f"offline-recharge:{req.pk}:approved",
            [req.user],
            title=f"Recharge {req.transaction_number} approved",
            message=(
                f"{_money(amount)} has been credited to your {req.department.name} wallet. Note: {note}"
            ),
            link=WALLET_LINK,
        )
    approved.refresh_from_db()
    return approved


def reject(request_id: int, actor, *, note) -> WalletRechargeRequest:
    from iic_booking.users.wallet_recharge_workflow import RechargeAlreadyProcessed, reject_request

    note = _note(note, label="Reason for rejection")
    with transaction.atomic():
        req = _lock(request_id)
        if not can_decide(actor, req):
            raise OfflineRechargeError("You cannot act on this request.", status=403, code="forbidden")
        detail = _lock_detail(req)
        if req.status != WalletRechargeRequestStatus.PENDING:
            raise _already(req)
        try:
            rejected = reject_request(
                req, reason_code=WalletRechargeRejectionReason.OTHER, reason_text=note, actor=actor
            )
        except RechargeAlreadyProcessed:
            raise _already(req) from None
        DepartmentPaymentReceipt.objects.filter(pk=detail.receipt_id).update(
            status=DepartmentPaymentReceiptStatus.REJECTED,
            utr_reference=_released_reference(detail),
            finance_processed_at=timezone.now(),
            finance_processed_by=actor,
            finance_remarks=note[:NOTE_MAX],
        )
        _notify(
            f"offline-recharge:{req.pk}:rejected",
            [req.user],
            title=f"Recharge {req.transaction_number} rejected",
            message=f"Your offline payment of {_money(detail.claimed_amount)} was not approved. Reason: {note}",
            link=WALLET_LINK,
        )
    return rejected


def cancel(request_id: int, user) -> WalletRechargeRequest:
    from iic_booking.users.wallet_recharge_workflow import cancel_request

    with transaction.atomic():
        req = _lock(request_id)
        if req.user_id != user.pk:
            raise OfflineRechargeError("Request not found.", status=404, code="not_found")
        detail = _lock_detail(req)
        if req.status != WalletRechargeRequestStatus.PENDING:
            raise _already(req)
        cancel_request(req, source=WalletRechargeCancellationSource.USER, actor=user, note="Cancelled by the requester")
        DepartmentPaymentReceipt.objects.filter(pk=detail.receipt_id).update(
            status=DepartmentPaymentReceiptStatus.REJECTED,
            utr_reference=_released_reference(detail),
            finance_remarks="Cancelled by the requester",
        )
    req.refresh_from_db()
    return req


def reconcile(request_id: int, actor, *, outcome, reference="", credit_date=None, note="") -> WalletRechargeRequest:
    from iic_booking.users.wallet_recharge_workflow import append_audit_log, verify_fund_receipt

    outcome = str(outcome or "").strip().lower()
    if outcome not in ("reconciled", "mismatch"):
        raise OfflineRechargeError("Choose Reconciled or Mismatch.", field="outcome")
    with transaction.atomic():
        req = _lock(request_id)
        if not can_reconcile(actor, req):
            raise OfflineRechargeError("You cannot reconcile this request.", status=403, code="forbidden")
        detail = _lock_detail(req)
        if req.status != WalletRechargeRequestStatus.APPROVED or detail.reconciliation_status not in (
            OfflineReconciliationStatus.AWAITING,
            OfflineReconciliationStatus.MISMATCH,
        ):
            raise OfflineRechargeError(
                "Only an approved request awaiting reconciliation (or flagged as a mismatch) can be reconciled.",
                status=409,
                code="not_reconcilable",
            )
        now = timezone.now()
        previous = detail.reconciliation_status
        if outcome == "reconciled":
            ref = _text({"r": reference}, "r", required=True, max_len=100, label="Bank / grant credit reference")
            when = parse_date(credit_date, field="credit_date", label="Date funds were credited")
            text = str(note or "").strip()[:NOTE_MAX]
            detail.reconciliation_status = OfflineReconciliationStatus.RECONCILED
            detail.reconciled_reference = ref
            detail.reconciled_credit_date = when
            detail.reconciliation_note = text
            verify_fund_receipt(req, actor=actor, remarks=f"Reconciled: ref {ref} credited {when.isoformat()}. {text}".strip())
            title = f"Recharge {req.transaction_number} reconciled"
            message = "The institute has matched your payment with the funds received. No action is needed."
        else:
            text = _note(note, label="Mismatch note")
            detail.reconciliation_status = OfflineReconciliationStatus.MISMATCH
            detail.reconciliation_note = text
            if str(reference or "").strip():
                detail.reconciled_reference = str(reference).strip()[:100]
            title = f"Recharge {req.transaction_number}: payment not yet matched"
            message = (
                "The accounts office could not yet match your payment with the funds received. "
                f"Note: {text} They may contact you for details."
            )
        detail.reconciled_by = actor
        detail.reconciled_at = now
        detail.save()
        append_audit_log(
            req,
            action=f"offline_payment_{outcome}",
            from_status=req.status,
            to_status=req.status,
            actor=actor,
            message=detail.reconciliation_note or f"Reconciled with ref {detail.reconciled_reference}",
            metadata={
                "from_reconciliation": previous,
                "to_reconciliation": detail.reconciliation_status,
                "reference": detail.reconciled_reference,
                "credit_date": detail.reconciled_credit_date.isoformat() if detail.reconciled_credit_date else None,
            },
        )
        _notify(f"offline-recharge:{req.pk}:{outcome}:{now.timestamp():.0f}", [req.user], title=title, message=message, link=WALLET_LINK)
    req.refresh_from_db()
    return req
