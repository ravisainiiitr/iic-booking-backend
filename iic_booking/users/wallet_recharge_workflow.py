"""
Transaction-safe wallet recharge approval workflow (SRIC email + admin actions).

Email is an approval interface only — no reply/IMAP parsing.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from iic_booking.communication.utils import get_frontend_absolute_url
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWallet,
    WalletRechargeCancellationSource,
    WalletRechargeCreditFacilityStatus,
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
            "message": "This wallet recharge request has already been approved. No further action is required.",
        }
    if status == WalletRechargeRequestStatus.REJECTED:
        return {
            "page_code": "already_rejected",
            "title": "Already Rejected",
            "message": "This wallet recharge request has already been rejected. No further action is required.",
        }
    if status == WalletRechargeRequestStatus.CANCELLED:
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


def build_action_urls(recharge_request: WalletRechargeRequest) -> tuple[str, str]:
    token = recharge_request.action_token or ""
    approve = get_frontend_absolute_url(f"/wallet/recharge-action/{token}/approve")
    reject = get_frontend_absolute_url(f"/wallet/recharge-action/{token}/reject")
    return approve, reject


def get_sric_recipient_emails() -> list[str]:
    settings_obj = WalletSricSettings.get_singleton()
    emails = _parse_sric_recipient_emails(settings_obj.recipient_emails or "")
    if emails:
        return emails
    # Fallback to ACCOUNTS_EMAIL so requests are never silently dropped
    fallback = (getattr(settings, "ACCOUNTS_EMAIL", "") or "").strip()
    return [fallback] if fallback and "@" in fallback else []


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


def serialize_request_public(recharge_request: WalletRechargeRequest) -> dict[str, Any]:
    """Safe payload for email action pages (no secrets beyond what's needed)."""
    status = recharge_request.status
    page = None
    if status != WalletRechargeRequestStatus.PENDING:
        page = already_processed_page(status, recharge_request.cancellation_source or "")
    return {
        "request_id": recharge_request.request_id_display,
        "id": recharge_request.id,
        "amount": str(recharge_request.amount),
        "user_name": recharge_request.user.name or recharge_request.user.email,
        "user_email": recharge_request.user.email,
        "employee_number": recharge_request.employee_number or (recharge_request.user.emp_id or ""),
        "user_department": recharge_request.user_department_name
        or (recharge_request.user.department.name if recharge_request.user.department_id else ""),
        "department_name": recharge_request.department.name if recharge_request.department_id else "",
        "department_grant_code": recharge_request.department_grant_code or "",
        "project_grant_code": recharge_request.project_grant_code or "",
        "project_name": recharge_request.project.name if recharge_request.project_id else "",
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
        "rejection_reason_choices": [
            {"value": c.value, "label": str(c.label)} for c in WalletRechargeRejectionReason
        ],
    }


def _lock_pending(recharge_request: WalletRechargeRequest) -> WalletRechargeRequest:
    locked = (
        WalletRechargeRequest.objects.select_for_update()
        .select_related("user", "wallet", "department", "project", "account_incharge")
        .get(pk=recharge_request.pk)
    )
    if locked.status != WalletRechargeRequestStatus.PENDING:
        page = already_processed_page(locked.status, locked.cancellation_source or "")
        raise RechargeAlreadyProcessed(locked.status, page["page_code"], page["message"])
    return locked


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

    description = f"Wallet recharge approved — {locked.request_id_display}"
    if locked.project_grant_code:
        description += f" (project grant {locked.project_grant_code})"

    sub_wallet, _ = SubWallet.objects.get_or_create(
        wallet=locked.wallet,
        department=locked.department,
        defaults={"balance": Decimal("0.00")},
    )
    sub_wallet.credit(locked.amount, description, related_user=locked.user)

    email = _actor_email(actor, actor_email) or "sric-approval"
    locked.status = WalletRechargeRequestStatus.APPROVED
    locked.approved_by_email = email
    locked.processed_by = actor if getattr(actor, "pk", None) else None
    locked.response_message = (response_message or "").strip()
    locked.responded_at = timezone.now()
    locked.credit_facility_status = WalletRechargeCreditFacilityStatus.INACTIVE
    locked.credit_facility_opted_in = False
    locked.save()

    append_audit_log(
        locked,
        action="approved",
        from_status=WalletRechargeRequestStatus.PENDING,
        to_status=WalletRechargeRequestStatus.APPROVED,
        actor=actor,
        actor_email=email,
        message=locked.response_message,
        metadata={"amount": str(locked.amount)},
    )
    return locked


@transaction.atomic
def reject_request(
    recharge_request: WalletRechargeRequest,
    *,
    reason_code: str,
    reason_text: str = "",
    actor=None,
    actor_email: str = "",
) -> WalletRechargeRequest:
    locked = _lock_pending(recharge_request)
    code = (reason_code or "").strip()
    valid = {c.value for c in WalletRechargeRejectionReason}
    if code not in valid:
        raise ValueError("Invalid rejection reason")
    text = (reason_text or "").strip()
    if code == WalletRechargeRejectionReason.OTHER and not text:
        raise ValueError("Please enter a rejection reason when selecting Others")

    label = REJECTION_REASON_LABELS.get(code, code)
    message = text if code == WalletRechargeRejectionReason.OTHER else (text or label)

    email = _actor_email(actor, actor_email) or "sric-rejection"
    locked.status = WalletRechargeRequestStatus.REJECTED
    locked.approved_by_email = email
    locked.processed_by = actor if getattr(actor, "pk", None) else None
    locked.rejection_reason_code = code
    locked.rejection_reason_text = text
    locked.response_message = message
    locked.responded_at = timezone.now()
    locked.credit_facility_status = WalletRechargeCreditFacilityStatus.INACTIVE
    locked.credit_facility_opted_in = False
    locked.save()

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
    locked.save()

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


def send_sric_approval_email(recharge_request: WalletRechargeRequest) -> int:
    """
    Send SRIC approval-interface email (Approve / Reject buttons).
    Returns number of recipients emailed.
    """
    populate_request_snapshots(recharge_request)
    recharge_request.refresh_from_db()
    recipients = get_sric_recipient_emails()
    if not recipients:
        logger.warning("No SRIC recipients configured for recharge request %s", recharge_request.id)
        return 0

    user = recharge_request.user
    name = user.name or user.email
    emp = recharge_request.employee_number or (user.emp_id or "—")
    amount = recharge_request.amount
    user_dept = recharge_request.user_department_name or "—"
    credit_grant = recharge_request.department_grant_code or "—"
    debit_grant = recharge_request.project_grant_code or "—"
    approve_url, reject_url = build_action_urls(recharge_request)

    subject = f"Urgent Wallet Recharge Request from {name} - {emp}"
    text_body = f"""Urgent Wallet Recharge Request

Amount of Recharge: ₹{amount}
Name of the User: {name}
Employee Number: {emp}
User Department: {user_dept}
Amount to be Credited to Grant: {credit_grant}
Project Grant Code for Debit: {debit_grant}

Request ID: {recharge_request.request_id_display}

Approve: {approve_url}
Reject: {reject_url}

This email is an approval interface only. Please do not reply.
"""
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><style>
body{{font-family:Arial,sans-serif;line-height:1.6;color:#333}}
.box{{max-width:640px;margin:0 auto;padding:24px;border:1px solid #ddd;border-radius:8px}}
.row{{margin:8px 0}} .label{{font-weight:bold;color:#555}}
.btn{{display:inline-block;padding:12px 28px;margin:8px;border-radius:6px;color:#fff;text-decoration:none;font-weight:bold}}
.ok{{background:#2e7d32}} .bad{{background:#c62828}}
.note{{margin-top:16px;padding:12px;background:#fff8e1;border:1px solid #ffe082;font-size:13px}}
</style></head><body><div class="box">
<h2>Urgent Wallet Recharge Request</h2>
<div class="row"><span class="label">Amount of Recharge:</span> ₹{amount}</div>
<div class="row"><span class="label">Name of the User:</span> {name}</div>
<div class="row"><span class="label">Employee Number:</span> {emp}</div>
<div class="row"><span class="label">User Department:</span> {user_dept}</div>
<div class="row"><span class="label">Amount to be Credited to Grant:</span> {credit_grant}</div>
<div class="row"><span class="label">Project Grant Code for Debit:</span> {debit_grant}</div>
<div class="row"><span class="label">Request ID:</span> {recharge_request.request_id_display}</div>
<p style="text-align:center;margin:28px 0">
  <a class="btn ok" href="{approve_url}">Approve</a>
  <a class="btn bad" href="{reject_url}">Reject</a>
</p>
<div class="note">This email is a secure approval interface only. Do not reply to this message.
If the request was already processed in the admin dashboard, these buttons will show the current status and will not change anything.</div>
</div></body></html>"""

    send_mail(
        subject=subject,
        message=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=recipients,
        html_message=html_body,
        fail_silently=False,
    )
    WalletRechargeRequest.objects.filter(pk=recharge_request.pk).update(sric_notification_sent=True)
    append_audit_log(
        recharge_request,
        action="sric_email_sent",
        from_status=WalletRechargeRequestStatus.PENDING,
        to_status=WalletRechargeRequestStatus.PENDING,
        message=f"Approval email sent to {', '.join(recipients)}",
        metadata={"recipients": recipients},
    )
    return len(recipients)


def notify_stakeholders_of_decision(recharge_request: WalletRechargeRequest) -> None:
    """Email requesting user, account in-charge, and department administrators."""
    from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
    from iic_booking.communication.styled_transactional_emails import (
        send_wallet_recharge_approved_faculty_email,
    )

    status_key = recharge_request.status  # APPROVED / REJECTED / CANCELLED
    try:
        if status_key == WalletRechargeRequestStatus.APPROVED:
            send_wallet_recharge_request_notifications(recharge_request, "APPROVED")
            try:
                send_wallet_recharge_approved_faculty_email(recharge_request)
            except Exception:
                logger.exception("Faculty approved email failed for %s", recharge_request.id)
        elif status_key == WalletRechargeRequestStatus.REJECTED:
            send_wallet_recharge_request_notifications(recharge_request, "REJECTED")
        elif status_key == WalletRechargeRequestStatus.CANCELLED:
            send_wallet_recharge_request_notifications(recharge_request, "CANCELLED")
    except Exception:
        logger.exception("Stakeholder notification failed for request %s", recharge_request.id)

    # Extra CC-style notes to in-charge + dept admins
    recipients: list[str] = []
    if recharge_request.account_incharge_id and recharge_request.account_incharge.email:
        recipients.append(recharge_request.account_incharge.email)
    else:
        for u in find_department_account_incharges(recharge_request.department):
            if u.email:
                recipients.append(u.email)
    for u in find_department_administrators(recharge_request.department):
        if u.email:
            recipients.append(u.email)
    # Deduplicate, exclude requester
    requester = (recharge_request.user.email or "").lower()
    unique = []
    seen = set()
    for e in recipients:
        key = e.strip().lower()
        if not key or key == requester or key in seen:
            continue
        seen.add(key)
        unique.append(e.strip())

    if not unique:
        return

    status_label = recharge_request.get_status_display()
    subject = (
        f"Wallet Recharge {status_label}: {recharge_request.request_id_display} "
        f"— {recharge_request.user.name or recharge_request.user.email}"
    )
    reason = ""
    if recharge_request.status == WalletRechargeRequestStatus.REJECTED:
        reason = f"\nRejection reason: {recharge_request.response_message or '—'}"
    body = f"""Wallet recharge request {recharge_request.request_id_display} is now {status_label}.

User: {recharge_request.user.name or recharge_request.user.email}
Employee Number: {recharge_request.employee_number or '—'}
Amount: ₹{recharge_request.amount}
Department (credit): {recharge_request.department.name if recharge_request.department_id else '—'}
Department Grant Code: {recharge_request.department_grant_code or '—'}
Project Grant Code: {recharge_request.project_grant_code or '—'}
Processed by: {recharge_request.approved_by_email or '—'}
{reason}
"""
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=unique,
            fail_silently=True,
        )
    except Exception:
        logger.exception("Failed CC emails for recharge %s", recharge_request.id)
