"""
Transaction-safe wallet recharge approval workflow (SRIC email + admin actions).

Email is an approval interface only — no reply parsing. Fund receipt is confirmed separately
from the SRIC cash-book (wallet_recharge_import / wallet_cashbook_mailbox).

SRIC decline of a Project Grant request (before or after approval) cancels it and treats the
amount as an auto-approved credit; the next approved recharge for the same wallet and
department recovers that credit first.

Running credit (outstanding decline credit, a negative sub-wallet balance, or an active
admin-approved credit facility): an SRIC approval of a Project Grant request does not credit the
wallet until the SRIC cash-book confirms the funds, and an SRIC decline only cancels the request —
no second credit is given.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives, get_connection, send_mail
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from iic_booking.communication.utils import get_frontend_absolute_url
from iic_booking.users.display import get_user_display_name
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    CASH_DEPOSIT_DECLINE_REASONS,
    PROJECT_GRANT_DECLINE_REASONS,
    SubWallet,
    WalletRechargeCancellationSource,
    WalletRechargeCreditFacilityStatus,
    WalletRechargeMode,
    WalletRechargeRejectionReason,
    WalletRechargeRequest,
    WalletRechargeRequestAuditLog,
    WalletRechargeRequestStatus,
)
from iic_booking.users.wallet_recharge_ops import (
    _parse_sric_recipient_emails,
    resolve_department_grant_code,
)
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

logger = logging.getLogger(__name__)

REJECTION_REASON_LABELS = dict(WalletRechargeRejectionReason.choices)


class RechargeAlreadyProcessed(Exception):
    """Raised when approve/reject/cancel is attempted on a non-pending request."""

    def __init__(self, status: str, page_code: str, message: str):
        self.status = status
        self.page_code = page_code
        self.message = message
        super().__init__(message)


def already_processed_page(status: str, cancellation_source: str = "") -> dict[str, str]:
    """Map terminal status to a user-facing page code / message for email links."""
    if status == WalletRechargeRequestStatus.APPROVED:
        return {
            "page_code": "already_approved",
            "title": "Already Approved",
            "message": (
                "This wallet recharge request has already been approved. "
                "It cannot be approved again."
            ),
        }
    if status == WalletRechargeRequestStatus.REJECTED:
        return {
            "page_code": "already_rejected",
            "title": "Already Declined",
            "message": "This wallet recharge request has already been declined. No further action is required.",
        }
    if status == WalletRechargeRequestStatus.CANCELLED:
        if cancellation_source == WalletRechargeCancellationSource.SRIC_DECLINED:
            return {
                "page_code": "declined_to_credit",
                "title": "Declined by SRIC",
                "message": (
                    "This request has already been declined by the SRIC Office and stands cancelled. "
                    "No further action is required."
                ),
            }
        if cancellation_source == WalletRechargeCancellationSource.DEPT_ADMIN:
            return {
                "page_code": "cancelled_by_dept_admin",
                "title": "Cancelled by Department Administrator",
                "message": "This request was cancelled by a Department Administrator. Email approval links are no longer valid.",
            }
        if cancellation_source == WalletRechargeCancellationSource.USER:
            return {
                "page_code": "cancelled_by_user",
                "title": "Cancelled by User",
                "message": "This request was cancelled by the requesting user. Email approval links are no longer valid.",
            }
        return {
            "page_code": "cancelled_by_admin",
            "title": "Cancelled by Administrator",
            "message": "This request was cancelled by an Administrator. Email approval links are no longer valid.",
        }
    return {
        "page_code": "unavailable",
        "title": "Request Unavailable",
        "message": "This wallet recharge request cannot be processed.",
    }


def _actor_email(actor=None, actor_email: str = "") -> str:
    if actor_email and str(actor_email).strip():
        return str(actor_email).strip()
    if actor is not None:
        return getattr(actor, "email", "") or ""
    return ""


def append_audit_log(
    request: WalletRechargeRequest,
    *,
    action: str,
    to_status: str,
    from_status: str = "",
    actor=None,
    actor_email: str = "",
    message: str = "",
    metadata: Optional[dict] = None,
) -> WalletRechargeRequestAuditLog:
    return WalletRechargeRequestAuditLog.objects.create(
        request=request,
        from_status=from_status or "",
        to_status=to_status,
        action=action,
        actor=actor if getattr(actor, "pk", None) else None,
        actor_email=_actor_email(actor, actor_email),
        message=message or "",
        metadata=metadata or {},
    )


def populate_request_snapshots(recharge_request: WalletRechargeRequest) -> None:
    """Fill audit snapshot fields from related objects (call before first submit email)."""
    user = recharge_request.user
    emp = (getattr(user, "emp_id", None) or "").strip()
    user_dept = getattr(user, "department", None)
    user_dept_name = (getattr(user_dept, "name", None) or "").strip() if user_dept else ""
    dept_grant = ""
    if recharge_request.department_id:
        dept_grant = resolve_department_grant_code(recharge_request.department)
    project_grant = ""
    if getattr(recharge_request, "recharge_mode", None) != WalletRechargeMode.DIRECT_CASH_DEPOSIT:
        if recharge_request.project_id:
            project_grant = (recharge_request.project.project_code or "").strip()
        elif (recharge_request.project_details or "").strip():
            project_grant = (recharge_request.project_details or "").strip()[:100]

    recharge_request.employee_number = emp
    recharge_request.user_department_name = user_dept_name
    recharge_request.department_grant_code = dept_grant
    recharge_request.project_grant_code = project_grant
    if not recharge_request.action_token:
        import secrets

        recharge_request.action_token = secrets.token_urlsafe(32)
    recharge_request.save(
        update_fields=[
            "employee_number",
            "user_department_name",
            "department_grant_code",
            "project_grant_code",
            "action_token",
            "updated_at",
        ]
    )


_APPROVER_LINK_SALT = "wallet-recharge-approver-link"


def build_action_urls(recharge_request: WalletRechargeRequest, approver_email: str = "") -> tuple[str, str]:
    """Approve / Decline links. With approver_email the links carry a signed reference to that address,
    so the approval (or decline) is recorded against the mailbox the link was sent to."""
    token = recharge_request.action_token or ""
    email = (approver_email or "").strip().lower()
    if token and email:
        # "." never occurs in token_urlsafe output or in a signing.dumps value.
        token = f"{token}.{signing.dumps(email, salt=f'{_APPROVER_LINK_SALT}:{token}')}"
    approve = get_frontend_absolute_url(f"/wallet/recharge-action/{token}/approve")
    reject = get_frontend_absolute_url(f"/wallet/recharge-action/{token}/reject")
    return approve, reject


def resolve_action_token(raw_token: str) -> tuple[str, str]:
    """Split an email-link token into (action_token, approver email from the signed reference or "")."""
    token, _, ref = (raw_token or "").partition(".")
    if not ref:
        return token, ""
    try:
        email = signing.loads(ref, salt=f"{_APPROVER_LINK_SALT}:{token}")
    except signing.BadSignature:
        return token, ""
    return token, email if isinstance(email, str) and "@" in email else ""


def get_sric_recipient_emails() -> list[str]:
    settings_obj = WalletSricSettings.get_singleton()
    emails = _parse_sric_recipient_emails(settings_obj.recipient_emails or "")
    if emails:
        return emails
    # Fallback to ACCOUNTS_EMAIL so requests are never silently dropped
    fallback = (getattr(settings, "ACCOUNTS_EMAIL", "") or "").strip()
    return [fallback] if fallback and "@" in fallback else []


def get_sric_bill_section_emails() -> list[str]:
    """Recipients for Direct Cash Deposit / Bank Transfer recharge requests."""
    settings_obj = WalletSricSettings.get_singleton()
    emails = _parse_sric_recipient_emails(getattr(settings_obj, "bill_section_emails", "") or "")
    if emails:
        return emails
    # Fall back to SRIC Office recipients, then ACCOUNTS_EMAIL
    return get_sric_recipient_emails()


def find_department_account_incharges(department) -> list:
    """Accounts In Charge users scoped to the selected department (or any FINANCE if none)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    qs = User.objects.filter(user_type=UserType.FINANCE, is_active=True)
    if department is not None:
        scoped = qs.filter(department_id=department.id)
        if scoped.exists():
            return list(scoped)
    return list(qs[:20])


def find_department_administrators(department) -> list:
    from django.contrib.auth import get_user_model

    User = get_user_model()
    if department is None:
        return []
    return list(
        User.objects.filter(
            user_type=UserType.DEPT_ADMIN,
            is_active=True,
            department_id=department.id,
        )
    )


def _is_cash_mode(recharge_request: WalletRechargeRequest) -> bool:
    return getattr(recharge_request, "recharge_mode", None) == WalletRechargeMode.DIRECT_CASH_DEPOSIT


def decline_converts_to_credit(recharge_request: WalletRechargeRequest) -> bool:
    """Project Grant declines become an auto-approved credit (admin switch, on by default)."""
    if _is_cash_mode(recharge_request):
        return False
    return bool(WalletSricSettings.get_singleton().decline_converts_to_credit)


def can_sric_decline(recharge_request: WalletRechargeRequest) -> bool:
    """Pending requests can always be declined; approved Project Grant requests until funds are confirmed."""
    if recharge_request.status == WalletRechargeRequestStatus.PENDING:
        return True
    return (
        recharge_request.status == WalletRechargeRequestStatus.APPROVED
        and not recharge_request.fund_receipt_verified
        and not (recharge_request.cashbook_receipt_no or "").strip()
        and (bool(recharge_request.wallet_credit_pending) or decline_converts_to_credit(recharge_request))
    )


def running_credit_summary(wallet, department_id) -> dict[str, Decimal]:
    """Credit currently in use on (wallet, department): SRIC-decline credit, overdraft, admin credit facility."""
    zero = Decimal("0.00")
    decline_credit = outstanding_decline_credit(wallet.pk, department_id) if department_id else zero
    balance = (
        SubWallet.objects.filter(wallet=wallet, department_id=department_id)
        .values_list("balance", flat=True)
        .first()
    )
    overdraft = -Decimal(balance) if balance is not None and balance < 0 else zero
    facility = zero
    try:
        from django.db.models import Sum

        from iic_booking.users.models.wallet_credit_facility import (
            ACTIVE_CREDIT_BLOCKING_STATUSES,
            WalletCreditFacility,
        )
        from iic_booking.users.wallet_credit_facility_v2 import feature_enabled

        if department_id and getattr(wallet, "user_id", None) and feature_enabled():
            facility = (
                WalletCreditFacility.objects.filter(
                    user_id=wallet.user_id,
                    department_id=department_id,
                    status__in=list(ACTIVE_CREDIT_BLOCKING_STATUSES),
                    outstanding_amount__gt=0,
                ).aggregate(total=Sum("outstanding_amount"))["total"]
                or zero
            )
    except Exception:
        logger.exception("Could not read wallet credit facility for wallet %s", getattr(wallet, "pk", None))
    return {
        "decline_credit": decline_credit,
        "overdraft": overdraft,
        "credit_facility": facility,
        "total": decline_credit + overdraft + facility,
    }


def has_running_credit(wallet, department_id) -> bool:
    return running_credit_summary(wallet, department_id)["total"] > 0


def decline_reason_choices(recharge_request: WalletRechargeRequest) -> list[dict[str, str]]:
    codes = CASH_DEPOSIT_DECLINE_REASONS if _is_cash_mode(recharge_request) else PROJECT_GRANT_DECLINE_REASONS
    return [{"value": c.value, "label": str(c.label)} for c in codes]


def serialize_request_public(recharge_request: WalletRechargeRequest) -> dict[str, Any]:
    """Safe payload for email action pages (no secrets beyond what's needed)."""
    status = recharge_request.status
    page = None
    if status != WalletRechargeRequestStatus.PENDING:
        page = already_processed_page(status, recharge_request.cancellation_source or "")
    return {
        "request_id": recharge_request.request_id_display,
        "transaction_number": getattr(
            recharge_request, "transaction_number", recharge_request.request_id_display
        ),
        "id": recharge_request.id,
        "amount": str(recharge_request.amount),
        "user_name": get_user_display_name(recharge_request.user),
        "user_email": recharge_request.user.email,
        "employee_number": recharge_request.employee_number or (recharge_request.user.emp_id or ""),
        "user_department": recharge_request.user_department_name
        or (recharge_request.user.department.name if recharge_request.user.department_id else ""),
        "user_phone": (getattr(recharge_request.user, "phone_number", None) or ""),
        "user_type": getattr(recharge_request.user, "user_type", "") or "",
        "department_name": recharge_request.department.name if recharge_request.department_id else "",
        "department_grant_code": recharge_request.department_grant_code or "",
        "project_grant_code": recharge_request.project_grant_code or "",
        "project_name": recharge_request.project.name if recharge_request.project_id else "",
        "recharge_mode": getattr(recharge_request, "recharge_mode", "") or WalletRechargeMode.PROJECT_GRANT,
        "recharge_mode_display": (
            recharge_request.get_recharge_mode_display()
            if hasattr(recharge_request, "get_recharge_mode_display")
            else ""
        ),
        "fund_receipt_verified": bool(getattr(recharge_request, "fund_receipt_verified", False)),
        "status": status,
        "status_display": recharge_request.get_status_display(),
        "rejection_reason_code": recharge_request.rejection_reason_code or "",
        "rejection_reason_text": recharge_request.rejection_reason_text or "",
        "response_message": recharge_request.response_message or "",
        "approved_by_email": recharge_request.approved_by_email or "",
        "responded_at": recharge_request.responded_at.isoformat() if recharge_request.responded_at else None,
        "created_at": recharge_request.created_at.isoformat() if recharge_request.created_at else None,
        "is_pending": status == WalletRechargeRequestStatus.PENDING,
        "terminal_page": page,
        "cancellation_source": recharge_request.cancellation_source or "",
        "can_decline": can_sric_decline(recharge_request),
        "decline_converts_to_credit": decline_converts_to_credit(recharge_request),
        "decline_credit_amount": str(recharge_request.decline_credit_amount or Decimal("0.00")),
        "decline_credit_outstanding": str(recharge_request.decline_credit_outstanding or Decimal("0.00")),
        "credit_settled_amount": str(recharge_request.credit_settled_amount or Decimal("0.00")),
        "wallet_credit_pending": bool(recharge_request.wallet_credit_pending),
        "wallet_credited_at": (
            recharge_request.wallet_credited_at.isoformat() if recharge_request.wallet_credited_at else None
        ),
        "rejection_reason_choices": decline_reason_choices(recharge_request),
    }


def _lock_pending(recharge_request: WalletRechargeRequest) -> WalletRechargeRequest:
    # of=("self",): Postgres rejects FOR UPDATE on nullable outer-join sides
    # (project / account_incharge / department may be null).
    locked = (
        WalletRechargeRequest.objects.select_for_update(of=("self",))
        .select_related("user", "wallet", "department", "project", "account_incharge")
        .get(pk=recharge_request.pk)
    )
    if locked.status != WalletRechargeRequestStatus.PENDING:
        page = already_processed_page(locked.status, locked.cancellation_source or "")
        raise RechargeAlreadyProcessed(locked.status, page["page_code"], page["message"])
    return locked


def _lock_for_decline(recharge_request: WalletRechargeRequest) -> WalletRechargeRequest:
    locked = (
        WalletRechargeRequest.objects.select_for_update(of=("self",))
        .select_related("user", "wallet", "department", "project", "account_incharge")
        .get(pk=recharge_request.pk)
    )
    if not can_sric_decline(locked):
        page = already_processed_page(locked.status, locked.cancellation_source or "")
        if locked.status == WalletRechargeRequestStatus.APPROVED and (
            locked.fund_receipt_verified or (locked.cashbook_receipt_no or "").strip()
        ):
            page = {
                **page,
                "message": (
                    "This request is already approved and the funds have been confirmed as received, "
                    "so it can no longer be declined."
                ),
            }
        raise RechargeAlreadyProcessed(locked.status, page["page_code"], page["message"])
    return locked


def _recover_outstanding_decline_credits(
    locked: WalletRechargeRequest, budget: Optional[Decimal] = None
) -> tuple[Decimal, list[str]]:
    """Apply this approval's amount to older SRIC-declined credits (same wallet + department), oldest first."""
    credits = list(
        WalletRechargeRequest.objects.select_for_update(of=("self",))
        .filter(
            wallet_id=locked.wallet_id,
            department_id=locked.department_id,
            decline_credit_outstanding__gt=0,
        )
        .exclude(pk=locked.pk)
        .order_by("responded_at", "pk")
    )
    remaining = Decimal(locked.amount) if budget is None else Decimal(budget)
    recovered = Decimal("0.00")
    refs: list[str] = []
    now = timezone.now()
    for credit in credits:
        if remaining <= 0:
            break
        take = min(remaining, credit.decline_credit_outstanding)
        credit.decline_credit_outstanding -= take
        fields = ["decline_credit_outstanding", "updated_at"]
        if credit.decline_credit_outstanding <= 0:
            credit.decline_credit_outstanding = Decimal("0.00")
            credit.decline_credit_settled_at = now
            fields.append("decline_credit_settled_at")
        credit.save(update_fields=fields)
        append_audit_log(
            credit,
            action="decline_credit_recovered",
            from_status=credit.status,
            to_status=credit.status,
            actor_email="system",
            message=f"₹{take} recovered from approved recharge {locked.transaction_number}.",
            metadata={
                "recovered_by_request_id": locked.pk,
                "amount": str(take),
                "outstanding": str(credit.decline_credit_outstanding),
            },
        )
        remaining -= take
        recovered += take
        refs.append(credit.transaction_number)
    return recovered, refs


@transaction.atomic
def approve_request(
    recharge_request: WalletRechargeRequest,
    *,
    response_message: str = "",
    actor=None,
    actor_email: str = "",
) -> WalletRechargeRequest:
    locked = _lock_pending(recharge_request)
    if not locked.user_otp_verified:
        raise ValueError("User OTP must be verified before approval")
    if not locked.department_id:
        raise ValueError("Department is required for recharge requests")

    running = running_credit_summary(locked.wallet, locked.department_id)
    defer = not _is_cash_mode(locked) and running["total"] > 0
    now = timezone.now()
    adjusted = {"decline_credit": Decimal("0.00"), "overdraft": Decimal("0.00"), "refs": []}
    if not defer:
        description = f"Wallet recharge approved — {locked.request_id_display}"
        if locked.project_grant_code:
            description += f" (project grant {locked.project_grant_code})"
        adjusted = _credit_wallet_and_adjust(locked, description)

    email = _actor_email(actor, actor_email) or "sric-approval"
    locked.status = WalletRechargeRequestStatus.APPROVED
    locked.approved_by_email = email
    locked.processed_by = actor if getattr(actor, "pk", None) else None
    locked.response_message = (response_message or "").strip()
    locked.responded_at = now
    locked.credit_facility_status = WalletRechargeCreditFacilityStatus.INACTIVE
    locked.credit_facility_opted_in = False
    locked.credit_settled_amount = adjusted["decline_credit"] + adjusted["overdraft"]
    locked.wallet_credit_pending = defer
    locked.wallet_credited_at = None if defer else now
    locked.save(
        update_fields=[
            "status",
            "approved_by_email",
            "processed_by",
            "response_message",
            "responded_at",
            "credit_facility_status",
            "credit_facility_opted_in",
            "credit_settled_amount",
            "wallet_credit_pending",
            "wallet_credited_at",
            "updated_at",
        ]
    )

    message = locked.response_message
    if defer:
        note = (
            f"Running credit ₹{running['total']:,.2f}: wallet will be credited when the SRIC cash-book "
            "confirms the funds."
        )
        message = f"{message} {note}".strip()
    append_audit_log(
        locked,
        action="approved_credit_deferred" if defer else "approved",
        from_status=WalletRechargeRequestStatus.PENDING,
        to_status=WalletRechargeRequestStatus.APPROVED,
        actor=actor,
        actor_email=email,
        message=message,
        metadata={
            "amount": str(locked.amount),
            "wallet_credit_deferred": defer,
            "running_credit": {k: str(v) for k, v in running.items()},
            "credit_recovered": str(adjusted["decline_credit"]),
            "overdraft_adjusted": str(adjusted["overdraft"]),
            "credit_refs": adjusted["refs"],
        },
    )
    return locked


def _credit_wallet_and_adjust(locked: WalletRechargeRequest, description: str) -> dict[str, Any]:
    """Credit the full amount, then settle running credit: overdraft first (via the balance), then decline credits."""
    amount = Decimal(locked.amount)
    sub_wallet, _ = SubWallet.objects.get_or_create(
        wallet=locked.wallet,
        department=locked.department,
        defaults={"balance": Decimal("0.00")},
    )
    balance_before = Decimal(sub_wallet.balance)
    sub_wallet.credit(amount, description, related_user=locked.user)
    overdraft_adjusted = min(amount, -balance_before) if balance_before < 0 else Decimal("0.00")

    budget = amount - overdraft_adjusted
    recovered, refs = (Decimal("0.00"), [])
    if budget > 0:
        recovered, refs = _recover_outstanding_decline_credits(locked, budget=budget)
    if recovered > 0:
        # The declined amount was already spendable; recovering it must never push the
        # balance below where it stood before this recharge.
        floor = min(Decimal("0.00"), sub_wallet.balance - budget)
        sub_wallet.debit(
            recovered,
            f"Credit recovered — {locked.transaction_number} adjusted against auto-approved credit "
            f"{', '.join(refs)}",
            related_user=locked.user,
            minimum_balance_after=floor,
        )
    return {"decline_credit": recovered, "overdraft": overdraft_adjusted, "refs": refs}


def apply_deferred_wallet_credit(
    locked: WalletRechargeRequest, *, actor=None, actor_email: str = "", source: str = ""
) -> dict[str, Any]:
    """Credit a deferred approval once SRIC funds are confirmed. Caller holds the row lock inside a transaction."""
    if not locked.wallet_credit_pending or locked.status != WalletRechargeRequestStatus.APPROVED:
        return {}
    adjusted = _credit_wallet_and_adjust(
        locked,
        f"Wallet recharge {locked.transaction_number} credited on SRIC fund receipt"
        + (f" ({source})" if source else ""),
    )
    locked.wallet_credit_pending = False
    locked.wallet_credited_at = timezone.now()
    locked.credit_settled_amount = adjusted["decline_credit"] + adjusted["overdraft"]
    locked.save(
        update_fields=["wallet_credit_pending", "wallet_credited_at", "credit_settled_amount", "updated_at"]
    )
    settled = locked.credit_settled_amount
    append_audit_log(
        locked,
        action="wallet_credited_on_fund_receipt",
        from_status=locked.status,
        to_status=locked.status,
        actor=actor,
        actor_email=_actor_email(actor, actor_email) or "system",
        message=(
            f"₹{locked.amount} credited after SRIC fund receipt; ₹{settled} adjusted against running credit."
        ),
        metadata={
            "amount": str(locked.amount),
            "credit_recovered": str(adjusted["decline_credit"]),
            "overdraft_adjusted": str(adjusted["overdraft"]),
            "credit_refs": adjusted["refs"],
            "source": source,
        },
    )
    return adjusted


@transaction.atomic
def reject_request(
    recharge_request: WalletRechargeRequest,
    *,
    reason_code: str,
    reason_text: str = "",
    actor=None,
    actor_email: str = "",
) -> WalletRechargeRequest:
    locked = _lock_for_decline(recharge_request)
    code = (reason_code or "").strip()
    valid = {c.value for c in WalletRechargeRejectionReason}
    if code not in valid:
        raise ValueError("Invalid rejection reason")
    text = (reason_text or "").strip()
    if code == WalletRechargeRejectionReason.OTHER and not text:
        raise ValueError("Please enter the reason when selecting Other")

    label = REJECTION_REASON_LABELS.get(code, code)
    message = text if code == WalletRechargeRejectionReason.OTHER else (text or label)

    email = _actor_email(actor, actor_email) or "sric-rejection"
    eligible = locked.user_otp_verified and locked.department_id and not _is_cash_mode(locked)
    if eligible and (
        locked.wallet_credit_pending
        or (
            locked.status == WalletRechargeRequestStatus.PENDING
            and decline_converts_to_credit(locked)
            and has_running_credit(locked.wallet, locked.department_id)
        )
    ):
        return _decline_to_credit(
            locked,
            code=code,
            text=text,
            message=message,
            label=label,
            actor=actor,
            email=email,
            new_credit=False,
        )
    if decline_converts_to_credit(locked) and locked.user_otp_verified and locked.department_id:
        return _decline_to_credit(
            locked, code=code, text=text, message=message, label=label, actor=actor, email=email
        )
    if locked.status != WalletRechargeRequestStatus.PENDING:
        page = already_processed_page(locked.status, locked.cancellation_source or "")
        raise RechargeAlreadyProcessed(locked.status, page["page_code"], page["message"])

    locked.status = WalletRechargeRequestStatus.REJECTED
    locked.approved_by_email = email
    locked.processed_by = actor if getattr(actor, "pk", None) else None
    locked.rejection_reason_code = code
    locked.rejection_reason_text = text
    locked.response_message = message
    locked.responded_at = timezone.now()
    locked.credit_facility_status = WalletRechargeCreditFacilityStatus.INACTIVE
    locked.credit_facility_opted_in = False
    locked.save(
        update_fields=[
            "status",
            "approved_by_email",
            "processed_by",
            "rejection_reason_code",
            "rejection_reason_text",
            "response_message",
            "responded_at",
            "credit_facility_status",
            "credit_facility_opted_in",
            "updated_at",
        ]
    )

    append_audit_log(
        locked,
        action="rejected",
        from_status=WalletRechargeRequestStatus.PENDING,
        to_status=WalletRechargeRequestStatus.REJECTED,
        actor=actor,
        actor_email=email,
        message=message,
        metadata={"rejection_reason_code": code},
    )
    return locked


def _decline_to_credit(
    locked: WalletRechargeRequest,
    *,
    code: str,
    text: str,
    message: str,
    label: str,
    actor,
    email: str,
    new_credit: bool = True,
) -> WalletRechargeRequest:
    """Cancel the request; with new_credit its amount becomes an auto-approved credit (no admin approval).

    new_credit=False is used when a credit is already running: the request is only cancelled and the
    wallet is not touched (a deferred approval was never credited).
    """
    from_status = locked.status
    was_pending = from_status == WalletRechargeRequestStatus.PENDING
    amount = Decimal(locked.amount) if new_credit else Decimal("0.00")
    running_total = (
        Decimal("0.00") if new_credit else running_credit_summary(locked.wallet, locked.department_id)["total"]
    )
    if was_pending and new_credit:
        sub_wallet, _ = SubWallet.objects.get_or_create(
            wallet=locked.wallet,
            department=locked.department,
            defaults={"balance": Decimal("0.00")},
        )
        sub_wallet.credit(
            amount,
            f"Auto-approved credit — {locked.transaction_number} declined by SRIC ({label})",
            related_user=locked.user,
        )

    locked.status = WalletRechargeRequestStatus.CANCELLED
    locked.cancellation_source = WalletRechargeCancellationSource.SRIC_DECLINED
    locked.approved_by_email = email
    locked.processed_by = actor if getattr(actor, "pk", None) else None
    locked.rejection_reason_code = code
    locked.rejection_reason_text = text
    locked.response_message = message
    locked.responded_at = timezone.now()
    locked.decline_credit_amount = amount
    locked.decline_credit_outstanding = amount
    locked.wallet_credit_pending = False
    locked.save(
        update_fields=[
            "status",
            "cancellation_source",
            "approved_by_email",
            "processed_by",
            "rejection_reason_code",
            "rejection_reason_text",
            "response_message",
            "responded_at",
            "decline_credit_amount",
            "decline_credit_outstanding",
            "wallet_credit_pending",
            "updated_at",
        ]
    )
    append_audit_log(
        locked,
        action="declined_to_credit" if new_credit else "declined_credit_already_running",
        from_status=from_status,
        to_status=WalletRechargeRequestStatus.CANCELLED,
        actor=actor,
        actor_email=email,
        message=message,
        metadata={
            "rejection_reason_code": code,
            "credit_amount": str(amount),
            "wallet_credited_now": was_pending and new_credit,
            "running_credit": str(running_total),
        },
    )
    return locked


@transaction.atomic
def cancel_request(
    recharge_request: WalletRechargeRequest,
    *,
    source: str,
    actor=None,
    actor_email: str = "",
    note: str = "",
) -> WalletRechargeRequest:
    locked = _lock_pending(recharge_request)
    # Unverified OTP drafts may still be hard-deleted for cleanup (no lasting audit row).
    if not locked.user_otp_verified and source == WalletRechargeCancellationSource.USER:
        locked.delete()
        return recharge_request

    email = _actor_email(actor, actor_email)
    locked.status = WalletRechargeRequestStatus.CANCELLED
    locked.cancellation_source = source
    locked.approved_by_email = email
    locked.processed_by = actor if getattr(actor, "pk", None) else None
    locked.response_message = (note or "").strip()
    locked.responded_at = timezone.now()
    locked.credit_facility_status = WalletRechargeCreditFacilityStatus.INACTIVE
    locked.credit_facility_opted_in = False
    locked.save(
        update_fields=[
            "status",
            "cancellation_source",
            "approved_by_email",
            "processed_by",
            "response_message",
            "responded_at",
            "credit_facility_status",
            "credit_facility_opted_in",
            "updated_at",
        ]
    )

    append_audit_log(
        locked,
        action="cancelled",
        from_status=WalletRechargeRequestStatus.PENDING,
        to_status=WalletRechargeRequestStatus.CANCELLED,
        actor=actor,
        actor_email=email,
        message=note,
        metadata={"cancellation_source": source},
    )
    return locked


def _mode_option(mode: str) -> str:
    return "direct_cash" if mode == WalletRechargeMode.DIRECT_CASH_DEPOSIT else "project_grant"


def get_recharge_cc_emails(mode: str, department=None) -> list[str]:
    """Configured CC addresses for the recharge mode (department override, else default, else SRIC settings)."""
    from iic_booking.users.wallet_payment_modes import configured_recipients, expand_recipients

    _, cc_tokens, _ = configured_recipients(_mode_option(mode), department)
    return _unique_emails(expand_recipients(cc_tokens, department=department))


def _unique_emails(emails, *, exclude=()) -> list[str]:
    seen = {(e or "").strip().lower() for e in exclude}
    out: list[str] = []
    for e in emails:
        value = (e or "").strip()
        if not value or "@" not in value or value.lower() in seen:
            continue
        seen.add(value.lower())
        out.append(value)
    return out


def route_for_test_requester(recharge_request: WalletRechargeRequest, emails: list[str]) -> list[str]:
    """Mail about a test account's request goes to the test-account inbox, never to real approvers."""
    from iic_booking.users.test_accounts import email_redirects, is_test_user

    if not emails or not is_test_user(getattr(recharge_request, "user", None)):
        return emails
    return email_redirects()


def get_recharge_copy_recipients(recharge_request: WalletRechargeRequest, *, exclude=()) -> list[str]:
    """Requester (always first), wallet owner, then configured CC addresses for the mode."""
    mode = getattr(recharge_request, "recharge_mode", None) or WalletRechargeMode.PROJECT_GRANT
    defaults = [getattr(recharge_request.user, "email", "") or ""]
    wallet_owner = getattr(getattr(recharge_request, "wallet", None), "user", None)
    if wallet_owner is not None:
        defaults.append(getattr(wallet_owner, "email", "") or "")
    cc = get_recharge_cc_emails(mode, getattr(recharge_request, "department_id", None))
    return _unique_emails(defaults + cc, exclude=exclude)


def get_recharge_approver_emails(recharge_request: WalletRechargeRequest) -> list[str]:
    """Who receives the Approve / Decline links: the configured To list for the department, which defaults
    to the Bill Section (cash) or SRIC Office (project grant). Never empty while those offices are set."""
    from iic_booking.users.wallet_payment_modes import configured_recipients, expand_recipients

    mode = getattr(recharge_request, "recharge_mode", None) or WalletRechargeMode.PROJECT_GRANT
    department_id = getattr(recharge_request, "department_id", None)
    wallet_owner = getattr(getattr(recharge_request, "wallet", None), "user", None)
    to_tokens, _, _ = configured_recipients(_mode_option(mode), department_id)
    # The links credit the wallet without login: never send them to the requester or the wallet owner.
    parties = [getattr(recharge_request.user, "email", "") or "", getattr(wallet_owner, "email", "") or ""]
    emails = _unique_emails(
        expand_recipients(to_tokens, department=department_id, wallet_owner=wallet_owner), exclude=parties
    )
    if emails:
        return emails
    return get_sric_bill_section_emails() if _is_cash_mode(recharge_request) else get_sric_recipient_emails()


def _requester_supervisor(recharge_request: WalletRechargeRequest):
    """Faculty owner of the joined wallet, else the user's recorded supervisor."""
    user = recharge_request.user
    owner = getattr(getattr(recharge_request, "wallet", None), "user", None)
    if owner is not None and owner.pk != user.pk:
        return owner
    return getattr(user, "supervisor", None)


def requester_is_supervisor(user) -> bool:
    if getattr(user, "user_type", "") == UserType.FACULTY:
        return True
    return bool(getattr(user, "pk", None)) and user.supervised_users.exists()


def requester_detail_rows(recharge_request: WalletRechargeRequest) -> list[tuple[str, str]]:
    """User Name, Enrollment Number, Department, Supervisor Name / Employee ID (faculty: own Employee ID,
    designation, and "Self" as supervisor)."""
    user = recharge_request.user
    name = get_user_display_name(user) or "—"
    own_id = (recharge_request.employee_number or user.emp_id or "").strip() or "—"
    dept = (
        recharge_request.user_department_name
        or (user.department.name if user.department_id else "")
        or ""
    ).strip() or "—"
    if requester_is_supervisor(user):
        return [
            ("User Name", name),
            ("Employee ID", own_id),
            ("Designation", (user.designation or "").strip() or "—"),
            ("Department", dept),
            ("Supervisor", "Self (the requester is the supervisor)"),
        ]
    supervisor = _requester_supervisor(recharge_request)
    id_label = "Enrollment Number" if user.user_type == UserType.STUDENT else "Enrollment / Employee ID"
    return [
        ("User Name", name),
        (id_label, own_id),
        ("Department", dept),
        ("Supervisor Name", (get_user_display_name(supervisor) or "—") if supervisor else "—"),
        ("Supervisor Employee ID", ((supervisor.emp_id or "").strip() or "—") if supervisor else "—"),
    ]


def requester_details_text(recharge_request: WalletRechargeRequest) -> str:
    return "\n".join(f"{label}: {value}" for label, value in requester_detail_rows(recharge_request))


def requester_details_html(recharge_request: WalletRechargeRequest) -> str:
    rows = "".join(
        f'<div style="margin:4px 0"><span style="font-weight:bold;color:#555">{escape(label)}:</span> '
        f"{escape(value)}</div>"
        for label, value in requester_detail_rows(recharge_request)
    )
    return (
        '<div style="margin:12px 0;padding:12px 14px;border:1px solid #cfd8dc;border-radius:8px;'
        'background:#f8fafc"><div style="font-weight:700;margin-bottom:6px">Requester details</div>'
        f"{rows}</div>"
    )


def decision_actor_display(recharge_request: WalletRechargeRequest) -> str:
    """Email address that approved / declined the request, with the channel it came through."""
    raw = (recharge_request.approved_by_email or "").strip()
    email = raw if "@" in raw else ""
    office = "SRIC Bill Section" if _is_cash_mode(recharge_request) else "SRIC Office"
    cashbook = raw.startswith("sric-cashbook") or (recharge_request.response_message or "").startswith(
        "Approved against SRIC cash-book receipt"
    )
    if cashbook:
        receipt = (recharge_request.cashbook_receipt_no or "").strip()
        via = "SRIC cash-book receipt" + (f" {receipt}" if receipt else "")
        if recharge_request.processed_by_id:
            via += ", matched on the IIC portal"
        else:
            via += ", matched automatically from the SRIC cash-book email"
    elif recharge_request.processed_by_id:
        via = "signed in to the IIC portal"
    elif raw.startswith("sric-email") or email:
        via = f"{office} email link"
    else:
        via = "approval link"
    if email:
        return f"{email} ({via})"
    if raw.startswith("sric-email"):
        return f"{office} email link (sent to {', '.join(get_recharge_approver_emails(recharge_request)) or '—'})"
    return via


_RECHARGE_EMAIL_CSS = """
body{font-family:Arial,sans-serif;line-height:1.6;color:#333}
.box{max-width:640px;margin:0 auto;padding:24px;border:1px solid #ddd;border-radius:8px}
.row{margin:8px 0} .label{font-weight:bold;color:#555}
.amount{font-size:28px;font-weight:800;color:#0d47a1;margin:12px 0 20px;letter-spacing:0.02em}
.grant-highlight{margin:8px 0 18px;padding:14px 16px;background:#e8f5e9;border:2px solid #2e7d32;border-radius:8px;font-size:15px;font-weight:700;color:#1b5e20;line-height:1.35}
.grant-debit{background:#fff3e0;border-color:#e65100;color:#bf360c}
.grant-code{display:block;margin-top:6px;font-size:26px;font-weight:800;letter-spacing:0.03em;color:#0d47a1}
.txn{font-size:16px;font-weight:700;color:#111;background:#e3f2fd;padding:10px 14px;border-radius:6px;display:inline-block;margin-bottom:12px}
.btn{display:inline-block;padding:12px 28px;margin:8px;border-radius:6px;color:#fff !important;text-decoration:none;font-weight:bold}
.ok{background:#2e7d32} .bad{background:#c62828}
.note{margin-top:16px;padding:12px;background:#fff8e1;border:1px solid #ffe082;font-size:13px}
.copy-banner{margin:0 0 16px;padding:10px 14px;background:#eceff1;border-left:4px solid #607d8b;font-size:13px}
"""


def recharge_email_parts(recharge_request: WalletRechargeRequest) -> dict[str, Any]:
    """Request details, guidance and cash-book note of the SRIC approval email (shared with its reminders)."""
    mode = getattr(recharge_request, "recharge_mode", None) or WalletRechargeMode.PROJECT_GRANT
    is_cash = mode == WalletRechargeMode.DIRECT_CASH_DEPOSIT

    user = recharge_request.user
    name = get_user_display_name(user)
    emp = recharge_request.employee_number or (user.emp_id or "—")
    amount = recharge_request.amount
    amount_str = f"{amount:,.2f}" if hasattr(amount, "__float__") else str(amount)
    user_dept = recharge_request.user_department_name or "—"
    credit_grant = recharge_request.department_grant_code or "—"
    debit_grant = recharge_request.project_grant_code or "—"
    mode_label = "Direct Cash Deposit / Bank Transfer" if is_cash else "Recharge via Project Grant"
    txn = getattr(recharge_request, "transaction_number", None) or recharge_request.request_id_display
    phone = (getattr(user, "phone_number", None) or "—").strip() or "—"
    email = (user.email or "—").strip()
    user_type = getattr(user, "user_type", "") or "—"
    dept_name = recharge_request.department.name if recharge_request.department_id else "—"
    approver_label = "SRIC Bill Section" if is_cash else "SRIC Office"

    subject = f"[{txn}] Wallet Recharge ₹{amount_str} — {name}"
    if is_cash:
        grant_lines_text = (
            f"Amount to be Credited to Grant: {credit_grant}\n"
            f"Recharge Mode: {mode_label}"
        )
        grant_rows_html = (
            f'<div class="grant-highlight">Amount to be Credited to Grant<br/>'
            f'<span class="grant-code">{escape(credit_grant)}</span></div>'
            f'<div class="row"><span class="label">Recharge Mode:</span> {mode_label}</div>'
        )
    else:
        grant_lines_text = (
            f"Amount to be Credited to Grant: {credit_grant}\n"
            f"PROJECT GRANT CODE FOR DEBIT: {debit_grant}"
        )
        grant_rows_html = (
            f'<div class="grant-highlight">Amount to be Credited to Grant<br/>'
            f'<span class="grant-code">{escape(credit_grant)}</span></div>'
            f'<div class="grant-highlight grant-debit">Project Grant Code for Debit<br/>'
            f'<span class="grant-code">{escape(debit_grant)}</span></div>'
        )

    if is_cash:
        person_text = f"""{requester_details_text(recharge_request)}
Email: {email}
Phone: {phone}
User type: {user_type}"""
        person_html = f"""{requester_details_html(recharge_request)}
<div class="row"><span class="label">Email:</span> {escape(email)}</div>
<div class="row"><span class="label">Phone:</span> {escape(phone)}</div>
<div class="row"><span class="label">User type:</span> {escape(user_type)}</div>"""
    else:
        person_text = f"""Name: {name}
Email: {email}
Phone: {phone}
Employee / ID: {emp}
User type: {user_type}
User department: {user_dept}"""
        person_html = f"""<div class="row"><span class="label">Name:</span> {escape(name)}</div>
<div class="row"><span class="label">Email:</span> {escape(email)}</div>
<div class="row"><span class="label">Phone:</span> {escape(phone)}</div>
<div class="row"><span class="label">Employee / ID:</span> {escape(emp)}</div>
<div class="row"><span class="label">User type:</span> {escape(user_type)}</div>
<div class="row"><span class="label">User department:</span> {escape(user_dept)}</div>"""

    details_text = f"""INTERNAL TRANSACTION NUMBER: {txn}
TOTAL AMOUNT: ₹{amount_str}

{grant_lines_text}

{person_text}
Credit department: {dept_name}"""

    details_html = f"""<div class="txn">Transaction ID: {escape(txn)}</div>
{grant_rows_html}
<div class="amount">Total amount: ₹{amount_str}</div>
{person_html}
<div class="row"><span class="label">Credit department:</span> {escape(dept_name)}</div>
<div class="row"><span class="label">Request ref:</span> {escape(recharge_request.request_id_display)}</div>"""

    if is_cash:
        guidance_text = (
            f"Accept the cash deposit only against this Transaction ID ({txn}) shown by the depositor.\n"
            "Click Approve after the amount is received; the wallet is credited immediately.\n"
            "Decline (with a reason) if the details do not match or no deposit is made.\n"
        )
        guidance_html = (
            f"Accept the cash deposit <strong>only</strong> against Transaction ID <strong>{escape(txn)}</strong> "
            "shown by the depositor. Click <strong>Approve</strong> after the amount is received; the wallet "
            "is credited immediately. <strong>Decline</strong> (with a reason) if the details do not match "
            "or no deposit is made."
        )
    else:
        guidance_text = (
            "Check that the project code is correct and the amount is available in the project head, then Approve;\n"
            "the wallet is credited immediately and the transfer is completed through the usual SRIC process.\n"
            "If the project code is wrong or funds are insufficient (now or later, even after approval), open\n"
            "Decline and choose the reason. The request is then cancelled and the amount is treated as an\n"
            "auto-approved credit for the faculty member, recovered from their next approved recharge.\n"
        )
        guidance_html = (
            "Check that the project code is correct and the amount is available in the project head, then "
            "<strong>Approve</strong>; the wallet is credited immediately and the transfer is completed through "
            "the usual SRIC process. If the project code is wrong or funds are insufficient (now or later, even "
            "after approval), open <strong>Decline</strong> and choose the reason. The request is then cancelled "
            "and the amount is treated as an auto-approved credit for the faculty member, recovered from their "
            "next approved recharge."
        )
    format_text = (
        f"When recording the transfer / receipt in the cash-book, write {txn} in the Payment Details column "
        "and the depositor's EMP NO in Received From, so the portal can confirm it automatically.\n"
    )
    format_html = (
        f"When recording the transfer / receipt in the cash-book, write <strong>{escape(txn)}</strong> in the "
        "<em>Payment Details</em> column and the depositor's <strong>EMP NO</strong> in <em>Received From</em>, "
        "so the portal can confirm it automatically."
    )
    return {
        "mode": mode,
        "is_cash": is_cash,
        "txn": txn,
        "name": name,
        "amount_str": amount_str,
        "mode_label": mode_label,
        "approver_label": approver_label,
        "subject": subject,
        "details_text": details_text,
        "details_html": details_html,
        "guidance_text": guidance_text,
        "guidance_html": guidance_html,
        "format_text": format_text,
        "format_html": format_html,
    }


def approver_email_body(
    parts: dict[str, Any], recipient: str, cc_text: str, approve_url: str = "", reject_url: str = ""
) -> tuple[str, str]:
    """Text and HTML body sent to one SRIC approver. Without action URLs the Approve / Decline block is left out."""
    if approve_url and reject_url:
        links_text = f"""Approve (credits wallet immediately): {approve_url}
Decline: {reject_url}

These links are personal to {recipient}: an approval or decline made from them is recorded and
reported as made by {recipient}.
"""
        links_html = f"""<p style="text-align:center;margin:28px 0">
  <a class="btn ok" href="{approve_url}">Approve</a>
  <a class="btn bad" href="{reject_url}">Decline</a>
</p>
<div class="note">These links are personal to <strong>{escape(recipient)}</strong>: an approval or decline
made from them is recorded and reported as made by {escape(recipient)}.</div>"""
    else:
        links_text = links_html = ""
    text_body = f"""Wallet Recharge Request — {parts["txn"]}

{parts["details_text"]}
Copy sent to (without action links): {cc_text}

{links_text}
{parts["guidance_text"]}
{parts["format_text"]}
If you Decline, you must provide a reason on the linked page.
Once approved, the request cannot be re-approved.
"""
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{_RECHARGE_EMAIL_CSS}</style></head><body><div class="box">
<h2>Wallet Recharge Request</h2>
{parts["details_html"]}
<div class="row"><span class="label">Copy sent to:</span> {escape(cc_text)}</div>
{links_html}
<div class="note">{parts["guidance_html"]}</div>
<div class="note">{parts["format_html"]}</div>
</div></body></html>"""
    return text_body, html_body


def send_sric_approval_email(recharge_request: WalletRechargeRequest) -> int:
    """
    Send approval-interface email (Approve / Decline buttons).
    Project Grant → SRIC Office recipients.
    Direct Cash Deposit → SRIC Bill Section.
    The requester, wallet owner and configured CC addresses get a copy without action links
    (the links credit the wallet without login, so they go to approvers only).
    Returns number of primary (approval) recipients emailed.
    """
    populate_request_snapshots(recharge_request)
    recharge_request.refresh_from_db()

    parts = recharge_email_parts(recharge_request)
    mode, is_cash, txn = parts["mode"], parts["is_cash"], parts["txn"]
    name, amount_str, mode_label = parts["name"], parts["amount_str"], parts["mode_label"]
    approver_label, subject = parts["approver_label"], parts["subject"]
    details_text, details_html = parts["details_text"], parts["details_html"]

    recipients = route_for_test_requester(recharge_request, get_recharge_approver_emails(recharge_request))
    if not recipients:
        logger.warning(
            "No %s recipients configured for recharge request %s",
            "Bill Section" if is_cash else "SRIC Office",
            recharge_request.id,
        )
        return 0
    copy_recipients = _unique_emails(
        route_for_test_requester(recharge_request, get_recharge_copy_recipients(recharge_request)),
        exclude=recipients,
    )
    cc_text = ", ".join(copy_recipients) if copy_recipients else "—"

    approval_messages = []
    for recipient in recipients:
        approve_url, reject_url = build_action_urls(recharge_request, approver_email=recipient)
        text_body, html_body = approver_email_body(parts, recipient, cc_text, approve_url, reject_url)
        message = EmailMultiAlternatives(
            subject=subject, body=text_body, from_email=settings.DEFAULT_FROM_EMAIL, to=[recipient]
        )
        message.attach_alternative(html_body, "text/html")
        approval_messages.append(message)
    get_connection(fail_silently=False).send_messages(approval_messages)

    if copy_recipients:
        sent_to = ", ".join(recipients)
        if is_cash:
            copy_subject = f"[{txn}] Wallet Recharge Submitted — next steps"
            next_steps_text = f"""
Next steps:
1. Visit the SRIC Bill Section to deposit cash (or complete the bank transfer).
2. Share this Transaction ID ({txn}) as your reference.
3. After approval, upload or update the payment receipt in your Wallet for final reconciliation.
"""
            next_steps_html = f"""<h3>Next steps</h3>
<ol>
<li>Visit the <strong>SRIC Bill Section</strong> to deposit cash (or complete the bank transfer).</li>
<li>Share Transaction ID <strong>{escape(txn)}</strong> as your reference.</li>
<li>After approval, upload or update the payment receipt in your Wallet for final reconciliation by Accounts.</li>
</ol>"""
        else:
            copy_subject = f"[{txn}] Wallet Recharge ₹{amount_str} — {name} (copy)"
            next_steps_text = ""
            next_steps_html = ""

        copy_text = f"""Wallet Recharge Request — {txn} (copy for your records)

This request has been sent to the {approver_label} for approval ({sent_to}).
Recharge Mode: {mode_label}

{details_text}
{next_steps_text}
You will receive a further email when the request is processed.
"""
        copy_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{_RECHARGE_EMAIL_CSS}</style></head><body><div class="box">
<h2>Wallet Recharge Request</h2>
<div class="copy-banner">Copy for your records. This request has been sent to the
<strong>{approver_label}</strong> for approval ({escape(sent_to)}).</div>
{details_html}
<div class="row"><span class="label">Recharge Mode:</span> {mode_label}</div>
{next_steps_html}
<p>You will receive a further email when the request is processed.</p>
</div></body></html>"""
        try:
            message = EmailMultiAlternatives(
                subject=copy_subject,
                body=copy_text,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=copy_recipients[:1],
                cc=copy_recipients[1:],
            )
            message.attach_alternative(copy_html, "text/html")
            message.send(fail_silently=True)
        except Exception:
            logger.exception("Failed to send recharge request copy for %s", recharge_request.id)

    WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(sric_notification_sent=True)
    append_audit_log(
        recharge_request,
        action="sric_email_sent" if not is_cash else "bill_section_email_sent",
        from_status=WalletRechargeRequestStatus.PENDING,
        to_status=WalletRechargeRequestStatus.PENDING,
        message=f"Approval email sent to {', '.join(recipients)}"
        + (f"; copy to {', '.join(copy_recipients)}" if copy_recipients else ""),
        metadata={
            "recipients": recipients,
            "cc": copy_recipients,
            "recharge_mode": mode,
            "transaction_number": txn,
        },
    )
    return len(recipients)


@transaction.atomic
def verify_fund_receipt(
    recharge_request: WalletRechargeRequest,
    *,
    actor,
    remarks: str = "",
) -> WalletRechargeRequest:
    """Department Account In-charge final financial verification (audit confirmation)."""
    locked = (
        WalletRechargeRequest.objects.select_for_update(of=("self",))
        .select_related("user", "wallet", "department", "fund_receipt_verified_by")
        .get(pk=recharge_request.pk)
    )
    if locked.fund_receipt_verified:
        raise ValueError("Fund receipt has already been verified for this request.")

    locked.fund_receipt_verified = True
    locked.fund_receipt_verified_by = actor if getattr(actor, "pk", None) else None
    locked.fund_receipt_verified_at = timezone.now()
    locked.fund_receipt_verification_remarks = (remarks or "").strip()
    locked.save(
        update_fields=[
            "fund_receipt_verified",
            "fund_receipt_verified_by",
            "fund_receipt_verified_at",
            "fund_receipt_verification_remarks",
            "updated_at",
        ]
    )
    append_audit_log(
        locked,
        action="fund_receipt_verified",
        from_status=locked.status,
        to_status=locked.status,
        actor=actor,
        actor_email=getattr(actor, "email", "") or "",
        message=locked.fund_receipt_verification_remarks or "Fund receipt verified",
        metadata={
            "verified_at": locked.fund_receipt_verified_at.isoformat()
            if locked.fund_receipt_verified_at
            else None
        },
    )
    if locked.wallet_credit_pending:
        apply_deferred_wallet_credit(locked, actor=actor, source="fund receipt verified")
        credited = locked
        transaction.on_commit(lambda: send_deferred_credit_applied_notification(credited))
    return locked


def overdue_fund_receipt_requests(queryset, days: Optional[int] = None, *, include_test: bool = False) -> tuple[int, list]:
    """Requests with no SRIC cash-book match `days` after approval (or after submission while still pending).

    Test-account requests never get a cash-book entry, so they are never overdue (``include_test`` is for reports).
    """
    from datetime import timedelta

    from django.db.models import Q

    from iic_booking.users.test_accounts import exclude_test_recharge_requests

    if days is None:
        days = WalletSricSettings.get_singleton().fund_receipt_overdue_days or 15
    cutoff = timezone.now() - timedelta(days=days)
    if not include_test:
        queryset = exclude_test_recharge_requests(queryset)
    rows = list(
        queryset.filter(fund_receipt_verified=False, cashbook_receipt_no="", is_deleted=False)
        .filter(
            Q(status=WalletRechargeRequestStatus.APPROVED, responded_at__lte=cutoff)
            | Q(status=WalletRechargeRequestStatus.PENDING, user_otp_verified=True, created_at__lte=cutoff)
        )
        .select_related("user", "department")
        .prefetch_related(None)
        .order_by("created_at")
    )
    return days, rows


def outstanding_decline_credit(wallet_id, department_id) -> Decimal:
    from django.db.models import Sum

    total = WalletRechargeRequest.objects.filter(
        wallet_id=wallet_id, department_id=department_id, decline_credit_outstanding__gt=0
    ).aggregate(total=Sum("decline_credit_outstanding"))["total"]
    return total or Decimal("0.00")


def send_decline_credit_notification(recharge_request: WalletRechargeRequest) -> None:
    """Tell the faculty member the request was declined and the amount is now an auto-approved credit."""
    txn = recharge_request.transaction_number
    user = recharge_request.user
    name = get_user_display_name(user)
    amount_str = f"{recharge_request.amount:,.2f}"
    reason = recharge_request.response_message or REJECTION_REASON_LABELS.get(
        recharge_request.rejection_reason_code, "—"
    )
    reason_label = REJECTION_REASON_LABELS.get(recharge_request.rejection_reason_code, "")
    if reason_label and reason_label != reason:
        reason = f"{reason_label}: {reason}"
    dept_name = recharge_request.department.name if recharge_request.department_id else "—"
    new_credit = (recharge_request.decline_credit_amount or Decimal("0.00")) > 0
    if new_credit:
        outstanding = outstanding_decline_credit(recharge_request.wallet_id, recharge_request.department_id)
    else:
        outstanding = running_credit_summary(recharge_request.wallet, recharge_request.department_id)["total"]
    outstanding_str = f"{outstanding:,.2f}"

    to = route_for_test_requester(
        recharge_request,
        _unique_emails([user.email, getattr(getattr(recharge_request.wallet, "user", None), "email", "")]),
    )
    if not to:
        return
    cc = _unique_emails(route_for_test_requester(recharge_request, get_sric_recipient_emails()), exclude=to)
    if new_credit:
        subject = f"[{txn}] Wallet Recharge Declined by SRIC — ₹{amount_str} treated as auto-approved credit"
        policy = (
            f"As per the IIC wallet policy, the request stands cancelled and the amount of ₹{amount_str} is "
            f"treated as an auto-approved credit facility on your {dept_name} wallet. No administrator "
            "approval is needed and your bookings are not interrupted."
        )
        policy_html = (
            f"As per the IIC wallet policy, the request stands cancelled and the amount of "
            f"<strong>₹{amount_str}</strong> is treated as an <strong>auto-approved credit facility</strong> "
            f"on your {escape(dept_name)} wallet. No administrator approval is needed and your bookings are "
            "not interrupted."
        )
        next_step = (
            "Please raise a fresh recharge request with the correct project details. When it is approved, "
            "the outstanding credit is adjusted automatically before any balance is added to your wallet."
        )
    else:
        subject = f"[{txn}] Wallet Recharge Declined by SRIC — request cancelled (credit already running)"
        policy = (
            f"As a credit facility is already running on your {dept_name} wallet, no new credit is given "
            "against this request. The request stands cancelled and your wallet balance is unchanged."
        )
        policy_html = (
            f"As a credit facility is <strong>already running</strong> on your {escape(dept_name)} wallet, "
            "<strong>no new credit is given</strong> against this request. The request stands cancelled "
            "and your wallet balance is unchanged."
        )
        next_step = (
            "Please raise a fresh recharge request with the correct project details. When its funds are "
            "received from the SRIC Office, the amount is credited and adjusted against the outstanding credit."
        )
    text = f"""Dear {name},

Your wallet recharge request {txn} for ₹{amount_str} (department: {dept_name}, project grant code:
{recharge_request.project_grant_code or '—'}) has been declined by the SRIC Office.

Reason: {reason}

{policy}

Outstanding credit on this wallet: ₹{outstanding_str}

{next_step}

Institute Instrumentation Centre, IIT Roorkee
"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{_RECHARGE_EMAIL_CSS}</style></head><body><div class="box">
<h2>Wallet Recharge Declined by SRIC</h2>
<div class="txn">Transaction ID: {escape(txn)}</div>
<p>Dear {escape(name)},</p>
<p>Your wallet recharge request for <strong>₹{amount_str}</strong> (department: {escape(dept_name)},
project grant code: {escape(recharge_request.project_grant_code or '—')}) has been
<strong>declined by the SRIC Office</strong>.</p>
<div class="grant-highlight grant-debit">Reason<span class="grant-code" style="font-size:18px">{escape(reason)}</span></div>
<p>{policy_html}</p>
<div class="amount">Outstanding credit: ₹{outstanding_str}</div>
<div class="note">{escape(next_step)}</div>
<p>Institute Instrumentation Centre, IIT Roorkee</p>
</div></body></html>"""
    message = EmailMultiAlternatives(
        subject=subject, body=text, from_email=settings.DEFAULT_FROM_EMAIL, to=to, cc=cc
    )
    message.attach_alternative(html, "text/html")
    message.send(fail_silently=True)


def send_credit_recovered_notification(recharge_request: WalletRechargeRequest) -> None:
    txn = recharge_request.transaction_number
    settled = recharge_request.credit_settled_amount or Decimal("0.00")
    net = Decimal(recharge_request.amount) - settled
    outstanding = outstanding_decline_credit(recharge_request.wallet_id, recharge_request.department_id)
    dept_name = recharge_request.department.name if recharge_request.department_id else "—"
    to = _unique_emails(
        [
            recharge_request.user.email,
            getattr(getattr(recharge_request.wallet, "user", None), "email", ""),
        ]
    )
    if not to:
        return
    body = f"""Your wallet recharge {txn} for ₹{recharge_request.amount:,.2f} ({dept_name}) has been approved.

₹{settled:,.2f} of it has been adjusted against the auto-approved credit from an earlier
SRIC-declined recharge request, and ₹{net:,.2f} has been added to your wallet balance.

Outstanding credit remaining on this wallet: ₹{outstanding:,.2f}

Institute Instrumentation Centre, IIT Roorkee
"""
    send_mail(
        subject=f"[{txn}] Recharge approved — ₹{settled:,.2f} adjusted against outstanding credit",
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=to,
        fail_silently=True,
    )


def _send_faculty_html(recharge_request: WalletRechargeRequest, subject: str, heading: str, paragraphs, amount_line: str) -> None:
    to = _unique_emails(
        [
            recharge_request.user.email,
            getattr(getattr(recharge_request.wallet, "user", None), "email", ""),
        ]
    )
    if not to:
        return
    name = get_user_display_name(recharge_request.user)
    txn = recharge_request.transaction_number
    text = "\n\n".join([f"Dear {name},", *paragraphs, amount_line, "Institute Instrumentation Centre, IIT Roorkee"])
    body_html = "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{_RECHARGE_EMAIL_CSS}</style></head><body><div class="box">
<h2>{escape(heading)}</h2>
<div class="txn">Transaction ID: {escape(txn)}</div>
<p>Dear {escape(name)},</p>
{body_html}
<div class="amount">{escape(amount_line)}</div>
<p>Institute Instrumentation Centre, IIT Roorkee</p>
</div></body></html>"""
    message = EmailMultiAlternatives(subject=subject, body=text, from_email=settings.DEFAULT_FROM_EMAIL, to=to)
    message.attach_alternative(html, "text/html")
    message.send(fail_silently=True)


def send_approved_pending_funds_notification(recharge_request: WalletRechargeRequest) -> None:
    """SRIC approved while a credit is running: wallet is credited only after the SRIC fund receipt."""
    txn = recharge_request.transaction_number
    amount_str = f"{recharge_request.amount:,.2f}"
    dept_name = recharge_request.department.name if recharge_request.department_id else "—"
    running = running_credit_summary(recharge_request.wallet, recharge_request.department_id)["total"]
    _send_faculty_html(
        recharge_request,
        f"[{txn}] Wallet Recharge Approved by SRIC — credit on receipt of funds",
        "Wallet Recharge Approved by SRIC",
        [
            f"Your wallet recharge request {txn} for ₹{amount_str} (department: {dept_name}, project grant "
            f"code: {recharge_request.project_grant_code or '—'}) has been approved by the SRIC Office.",
            "As a credit facility is currently running on this wallet, the amount will be credited to your "
            "wallet only after the SRIC Office confirms the actual transfer of funds (SRIC cash-book email). "
            "At that time it is adjusted against your outstanding credit and any balance is added to your wallet.",
            "No action is needed from you.",
        ],
        f"Outstanding credit today: ₹{running:,.2f}",
    )


def send_deferred_credit_applied_notification(recharge_request: WalletRechargeRequest) -> None:
    txn = recharge_request.transaction_number
    amount = Decimal(recharge_request.amount)
    settled = recharge_request.credit_settled_amount or Decimal("0.00")
    dept_name = recharge_request.department.name if recharge_request.department_id else "—"
    running = running_credit_summary(recharge_request.wallet, recharge_request.department_id)["total"]
    _send_faculty_html(
        recharge_request,
        f"[{txn}] Funds received — ₹{amount:,.2f} credited, ₹{settled:,.2f} adjusted against credit",
        "Wallet Recharge Credited",
        [
            f"The SRIC Office has confirmed receipt of funds for your wallet recharge {txn} "
            f"(₹{amount:,.2f}, department: {dept_name}).",
            f"₹{amount:,.2f} has been credited to your wallet. ₹{settled:,.2f} of it has been adjusted against "
            f"your outstanding credit and ₹{amount - settled:,.2f} has been added to your available balance.",
        ],
        f"Outstanding credit remaining: ₹{running:,.2f}",
    )


def approval_result_message(recharge_request: WalletRechargeRequest) -> str:
    amount = recharge_request.amount
    if recharge_request.wallet_credit_pending:
        return (
            f"Approved. The faculty member has a running credit, so ₹{amount} will be credited to the wallet "
            "and adjusted against that credit only after the SRIC cash-book confirms receipt of the funds."
        )
    message = f"Approved. ₹{amount} credited to the department wallet."
    if (recharge_request.credit_settled_amount or Decimal("0.00")) > 0:
        message += (
            f" ₹{recharge_request.credit_settled_amount} of it was adjusted against the faculty member's "
            "outstanding credit."
        )
    return message


def decline_result_message(recharge_request: WalletRechargeRequest) -> str:
    if recharge_request.status != WalletRechargeRequestStatus.CANCELLED:
        return "Wallet recharge request declined."
    if (recharge_request.decline_credit_amount or Decimal("0.00")) > 0:
        return (
            "Declined. The request is cancelled and the amount is treated as an auto-approved credit for the "
            "faculty member, who has been informed with the selected reason."
        )
    return (
        "Declined. The faculty member already has a running credit, so no new credit is given; the request "
        "stands cancelled and the faculty member has been informed with the selected reason."
    )


def notify_stakeholders_of_decision(recharge_request: WalletRechargeRequest) -> None:
    """Email requesting user, account in-charge, and department administrators.

    Never raises — approval/reject/cancel must succeed even if mail fails.
    """
    try:
        from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
        from iic_booking.communication.styled_transactional_emails import (
            send_wallet_recharge_approved_faculty_email,
        )

        status_key = recharge_request.status  # APPROVED / REJECTED / CANCELLED
        declined_to_credit = (
            status_key == WalletRechargeRequestStatus.CANCELLED
            and recharge_request.cancellation_source == WalletRechargeCancellationSource.SRIC_DECLINED
        )
        credit_deferred = (
            status_key == WalletRechargeRequestStatus.APPROVED and bool(recharge_request.wallet_credit_pending)
        )
        no_new_credit = declined_to_credit and not (recharge_request.decline_credit_amount or Decimal("0.00")) > 0
        try:
            if declined_to_credit:
                send_decline_credit_notification(recharge_request)
            elif credit_deferred:
                send_approved_pending_funds_notification(recharge_request)
            elif status_key == WalletRechargeRequestStatus.APPROVED:
                send_wallet_recharge_request_notifications(recharge_request, "APPROVED")
                try:
                    send_wallet_recharge_approved_faculty_email(recharge_request)
                except Exception:
                    logger.exception("Faculty approved email failed for %s", recharge_request.id)
                if (recharge_request.credit_settled_amount or Decimal("0.00")) > 0:
                    send_credit_recovered_notification(recharge_request)
            elif status_key == WalletRechargeRequestStatus.REJECTED:
                send_wallet_recharge_request_notifications(recharge_request, "REJECTED")
            elif status_key == WalletRechargeRequestStatus.CANCELLED:
                send_wallet_recharge_request_notifications(recharge_request, "CANCELLED")
        except Exception:
            logger.exception("Stakeholder notification failed for request %s", recharge_request.id)

        # Extra CC-style notes to in-charge + dept admins
        recipients: list[str] = []
        incharge = None
        try:
            incharge = (
                recharge_request.account_incharge
                if getattr(recharge_request, "account_incharge_id", None)
                else None
            )
        except Exception:
            incharge = None
        if incharge and getattr(incharge, "email", None):
            recipients.append(incharge.email)
        elif recharge_request.department_id:
            for u in find_department_account_incharges(recharge_request.department):
                if u.email:
                    recipients.append(u.email)
        if recharge_request.department_id:
            for u in find_department_administrators(recharge_request.department):
                if u.email:
                    recipients.append(u.email)
        try:
            # Approvals and plain declines also go back to the office that received the action links
            # (SRIC-declined credits already copy the SRIC Office on the faculty email).
            if status_key in (WalletRechargeRequestStatus.APPROVED, WalletRechargeRequestStatus.REJECTED):
                recipients.extend(get_recharge_approver_emails(recharge_request))
            recipients.extend(
                get_recharge_cc_emails(
                    getattr(recharge_request, "recharge_mode", None) or WalletRechargeMode.PROJECT_GRANT,
                    recharge_request.department_id,
                )
            )
        except Exception:
            logger.exception("Could not load recharge CC emails for %s", recharge_request.id)
        # Deduplicate, exclude requester
        requester = ""
        try:
            requester = (recharge_request.user.email or "").lower()
        except Exception:
            requester = ""
        unique = []
        seen = set()
        for e in recipients:
            key = (e or "").strip().lower()
            if not key or key == requester or key in seen:
                continue
            seen.add(key)
            unique.append(e.strip())

        unique = route_for_test_requester(recharge_request, unique)
        if not unique:
            return

        status_label = recharge_request.get_status_display()
        if no_new_credit:
            status_label = "Declined by SRIC (cancelled, credit already running)"
        elif declined_to_credit:
            status_label = "Declined by SRIC (auto-approved credit)"
        elif credit_deferred:
            status_label = "Approved by SRIC (wallet credit on fund receipt)"
        txn = getattr(recharge_request, "transaction_number", None) or recharge_request.request_id_display
        amount = recharge_request.amount
        amount_str = f"{amount:,.2f}" if hasattr(amount, "__float__") else str(amount)
        subject = (
            f"[{txn}] Wallet Recharge {status_label} — "
            f"{getattr(recharge_request.user, 'name', None) or getattr(recharge_request.user, 'email', '')}"
        )
        reason = ""
        if recharge_request.status == WalletRechargeRequestStatus.REJECTED or declined_to_credit:
            reason_label = REJECTION_REASON_LABELS.get(recharge_request.rejection_reason_code, "")
            reason = f"\nDecline reason: {reason_label or '—'}"
            if recharge_request.response_message and recharge_request.response_message != reason_label:
                reason += f" — {recharge_request.response_message}"
        if no_new_credit:
            reason += (
                "\nThe faculty member already has a running credit on this wallet, so no new credit is given. "
                "The request stands cancelled."
            )
        elif declined_to_credit:
            reason += (
                f"\nThe request is cancelled and ₹{amount_str} is treated as an auto-approved credit, "
                "recovered from the faculty member's next approved recharge for this department."
            )
        if credit_deferred:
            reason += (
                "\nThe faculty member has a running credit, so the wallet is credited only after the SRIC "
                "cash-book confirms the funds; the amount is then adjusted against the outstanding credit."
            )
        settled = recharge_request.credit_settled_amount or Decimal("0.00")
        if recharge_request.status == WalletRechargeRequestStatus.APPROVED and settled > 0:
            reason += (
                f"\nAdjusted against outstanding auto-approved credit: ₹{settled:,.2f} "
                f"(net added to wallet: ₹{Decimal(amount) - settled:,.2f})"
            )
        dept_name = "—"
        try:
            if recharge_request.department_id and recharge_request.department:
                dept_name = recharge_request.department.name
        except Exception:
            dept_name = "—"
        if status_key == WalletRechargeRequestStatus.APPROVED:
            actor_label = "Approved by"
        elif status_key == WalletRechargeRequestStatus.REJECTED or declined_to_credit:
            actor_label = "Declined by"
        else:
            actor_label = "Processed by"
        actor_value = decision_actor_display(recharge_request)
        user_name = getattr(recharge_request.user, "name", None) or getattr(recharge_request.user, "email", "")
        rows: list[tuple[str, str]] = [("Transaction ID", txn), ("TOTAL AMOUNT", f"₹{amount_str}")]
        if _is_cash_mode(recharge_request):
            rows += requester_detail_rows(recharge_request)
            rows.append(("Recharge Mode", "Direct Cash Deposit / Bank Transfer"))
        else:
            rows += [("User", user_name), ("Employee Number", recharge_request.employee_number or "—")]
        rows += [
            ("Department (credit)", dept_name),
            ("Department Grant Code", recharge_request.department_grant_code or "—"),
            ("Project Grant Code", recharge_request.project_grant_code or "—"),
        ]
        rows_text = "\n".join(f"{label}: {value}" for label, value in rows)
        body = f"""Wallet recharge request {txn} is now {status_label}.

{actor_label}: {actor_value}

{rows_text}
{reason}
"""
        rows_html = "".join(
            f'<div class="row"><span class="label">{escape(label)}:</span> {escape(value)}</div>'
            for label, value in rows
        )
        reason_html = "".join(f"<p>{escape(line)}</p>" for line in reason.strip().splitlines() if line.strip())
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>{_RECHARGE_EMAIL_CSS}</style></head><body><div class="box">
<h2>Wallet Recharge {escape(status_label)}</h2>
<div class="txn">Transaction ID: {escape(txn)}</div>
<div class="grant-highlight">{escape(actor_label)}
<span class="grant-code" style="font-size:18px">{escape(actor_value)}</span></div>
{rows_html}
{reason_html}
</div></body></html>"""
        try:
            message = EmailMultiAlternatives(
                subject=subject, body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=unique
            )
            message.attach_alternative(html, "text/html")
            message.send(fail_silently=True)
        except Exception:
            logger.exception("Failed CC emails for recharge %s", recharge_request.id)
    except Exception:
        logger.exception(
            "notify_stakeholders_of_decision failed for request %s",
            getattr(recharge_request, "id", None),
        )
