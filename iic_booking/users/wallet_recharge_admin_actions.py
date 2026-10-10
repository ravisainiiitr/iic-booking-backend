"""
Main Administrator actions on wallet recharge requests: soft delete and SRIC reminders.

Delete never touches money: a request that has credited a wallet (approved, deferred credit
applied, or SRIC-declined into a credit) is blocked and must be corrected in the Wallet ledger
first. A pending request is cancelled (its email links stop working) and then hidden.

A reminder repeats the original SRIC approval email (same details, guidance and, while still
pending, personal Approve / Decline links) with "Reminder #N" and an optional note; the
requester and any extra CC get a copy without action links.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from iic_booking.users.models.wallet import (
    WalletRechargeCancellationSource,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.wallet_recharge_workflow import (
    _unique_emails,
    append_audit_log,
    approver_email_body,
    build_action_urls,
    cancel_request,
    get_recharge_approver_emails,
    recharge_email_parts,
    route_for_test_requester,
)

logger = logging.getLogger(__name__)

REMINDER_COOLDOWN = timedelta(minutes=10)
MAX_EXTRA_CC = 10
CREDITED_BLOCK_MESSAGE = "This request has credited the wallet. Use Wallet ledger to debit before deleting."
DEFERRED_BLOCK_MESSAGE = (
    "This request is approved and its wallet credit is due when the funds are received. "
    "Decline it instead of deleting."
)


class RechargeAdminActionError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "", extra: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}


def has_credited_wallet(req: WalletRechargeRequest) -> bool:
    return bool(
        (req.status == WalletRechargeRequestStatus.APPROVED and not req.wallet_credit_pending)
        or req.wallet_credited_at
        or (req.decline_credit_amount or Decimal("0")) > 0
    )


def delete_blocked_reason(req: WalletRechargeRequest) -> str:
    if req.is_deleted:
        return "This request is already deleted."
    if has_credited_wallet(req):
        return CREDITED_BLOCK_MESSAGE
    if req.status == WalletRechargeRequestStatus.APPROVED:
        return DEFERRED_BLOCK_MESSAGE
    return ""


def _send_deletion_notice(req: WalletRechargeRequest, reason: str) -> None:
    to = route_for_test_requester(req, _unique_emails([getattr(req.user, "email", "")]))
    if not to:
        return
    txn = req.transaction_number
    amount = f"{req.amount:,.2f}"
    submitted = timezone.localtime(req.created_at).strftime("%d-%m-%Y") if req.created_at else "—"
    text = (
        f"Your wallet recharge request {txn} for ₹{amount}, submitted on {submitted}, has been withdrawn "
        f"by the IIC Main Administrator.\n\nReason: {reason}\n\n"
        "No amount was credited to or debited from your wallet for this request. "
        "Please raise a new request if a recharge is still required.\n"
    )
    html = (
        f"<p>Your wallet recharge request <strong>{escape(txn)}</strong> for ₹{amount}, submitted on "
        f"{submitted}, has been withdrawn by the IIC Main Administrator.</p>"
        f"<p><strong>Reason:</strong> {escape(reason)}</p>"
        "<p>No amount was credited to or debited from your wallet for this request. "
        "Please raise a new request if a recharge is still required.</p>"
    )
    try:
        message = EmailMultiAlternatives(
            subject=f"[{txn}] Wallet recharge request withdrawn",
            body=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to,
        )
        message.attach_alternative(html, "text/html")
        message.send(fail_silently=True)
    except Exception:
        logger.exception("Deletion notice failed for WRR-%s", req.pk)


def soft_delete_request(
    req: WalletRechargeRequest, *, actor, reason: str, inform_requester: bool = False
) -> WalletRechargeRequest:
    reason = (reason or "").strip()
    if not reason:
        raise RechargeAdminActionError("Enter the reason for deleting this request.")
    with transaction.atomic():
        locked = (
            WalletRechargeRequest.objects.select_for_update(of=("self",))
            .select_related("user", "wallet", "department")
            .get(pk=req.pk)
        )
        blocked = delete_blocked_reason(locked)
        if blocked:
            raise RechargeAdminActionError(
                blocked, status=409, code="CREDITED" if blocked == CREDITED_BLOCK_MESSAGE else "BLOCKED"
            )
        from_status = locked.status
        if locked.status == WalletRechargeRequestStatus.PENDING:
            locked = cancel_request(
                locked,
                source=WalletRechargeCancellationSource.ADMIN,
                actor=actor,
                note=f"Deleted by Main Administrator: {reason}",
            )
        released = (locked.cashbook_receipt_no or "").strip()
        now = timezone.now()
        locked.cashbook_parse_entry = None
        locked.cashbook_receipt_no = ""
        locked.cashbook_receipt_date = None
        locked.cashbook_matched_at = None
        locked.is_deleted = True
        locked.deleted_at = now
        locked.deleted_by = actor if getattr(actor, "pk", None) else None
        locked.deletion_reason = reason[:2000]
        locked.save(
            update_fields=[
                "cashbook_parse_entry",
                "cashbook_receipt_no",
                "cashbook_receipt_date",
                "cashbook_matched_at",
                "is_deleted",
                "deleted_at",
                "deleted_by",
                "deletion_reason",
                "updated_at",
            ]
        )
        append_audit_log(
            locked,
            action="deleted",
            from_status=from_status,
            to_status=locked.status,
            actor=actor,
            message=reason,
            metadata={"released_cashbook_receipt": released, "inform_requester": bool(inform_requester)},
        )
        if inform_requester:
            deleted = locked
            transaction.on_commit(lambda: _send_deletion_notice(deleted, reason))
    return locked


def reminder_blocked_reason(req: WalletRechargeRequest) -> str:
    if req.is_deleted:
        return "This request is deleted."
    if req.status == WalletRechargeRequestStatus.PENDING:
        if not req.user_otp_verified:
            return "The requester has not confirmed this request yet, so it was never sent to the SRIC office."
        return ""
    if (
        req.status == WalletRechargeRequestStatus.APPROVED
        and not req.fund_receipt_verified
        and not (req.cashbook_receipt_no or "").strip()
    ):
        from iic_booking.users.test_accounts import recharge_request_is_test

        if recharge_request_is_test(req):
            return "This request is from a test account (not counted in revenue); no SRIC cash-book entry is expected."
        return ""
    return "Reminders can be sent only for pending requests, or approved requests whose funds are not yet received."


def reminder_cooldown_seconds(req: WalletRechargeRequest, now=None) -> int:
    if not req.sric_reminder_last_sent_at:
        return 0
    left = req.sric_reminder_last_sent_at + REMINDER_COOLDOWN - (now or timezone.now())
    return max(0, int(left.total_seconds()))


def clean_extra_cc(raw: Any) -> list[str]:
    if isinstance(raw, str):
        import re

        items = re.split(r"[\s,;]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = []
    out = _unique_emails([x.strip() for x in items if x and x.strip()])
    invalid = []
    for email in out:
        try:
            validate_email(email)
        except ValidationError:
            invalid.append(email)
    bad = [x.strip() for x in items if x and x.strip() and "@" not in x]
    if invalid or bad:
        raise RechargeAdminActionError(f"Invalid CC address: {', '.join(invalid + bad)}")
    if len(out) > MAX_EXTRA_CC:
        raise RechargeAdminActionError(f"At most {MAX_EXTRA_CC} extra CC addresses are allowed.")
    return out


def reminder_recipients(req: WalletRechargeRequest, extra_cc: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """To: the same SRIC Office / Bill Section approvers as the original email. CC: requester, then extra CC."""
    to = route_for_test_requester(req, get_recharge_approver_emails(req))
    cc = _unique_emails(
        route_for_test_requester(req, _unique_emails([getattr(req.user, "email", "")] + list(extra_cc))),
        exclude=to,
    )
    return to, cc


def _local(dt) -> str:
    return timezone.localtime(dt).strftime("%d-%m-%Y %H:%M") if dt else "—"


def _reminder_banner(req: WalletRechargeRequest, parts: dict, number: int, note: str) -> tuple[str, str]:
    txn = parts["txn"]
    if req.status == WalletRechargeRequestStatus.PENDING:
        ask = (
            f"This wallet recharge request was sent to the {parts['approver_label']} on {_local(req.created_at)} "
            "and is still awaiting action. Please approve or decline it using the buttons below."
        )
    else:
        ask = (
            f"This request was approved on {_local(req.responded_at)}, but the funds have not yet appeared in the "
            f"SRIC cash-book. Please complete the transfer / receipt and record it quoting {txn} in Payment Details."
        )
    text = f"REMINDER #{number} — action pending\n{ask}\n"
    html = (
        '<div class="note" style="border-color:#ef6c00;background:#fff3e0;margin-bottom:16px">'
        f"<strong>Reminder #{number}</strong> — {escape(ask)}"
    )
    if note:
        text += f"Note from IIC: {note}\n"
        html += f'<div style="margin-top:8px"><strong>Note from IIC:</strong> {escape(note)}</div>'
    html += "</div>"
    return text, html


def _with_banner(text: str, html: str, banner_text: str, banner_html: str, number: int) -> tuple[str, str]:
    text = f"{banner_text}\n{text}"
    html = html.replace(
        "<h2>Wallet Recharge Request</h2>",
        f"<h2>Wallet Recharge Request — Reminder #{number}</h2>{banner_html}",
        1,
    )
    return text, html


def build_sric_reminder(
    req: WalletRechargeRequest,
    *,
    note: str = "",
    extra_cc: Iterable[str] = (),
    number: Optional[int] = None,
    preview: bool = True,
) -> dict[str, Any]:
    """Messages for one reminder. With preview the action links are placeholders (real links are personal)."""
    number = number or (req.sric_reminder_count or 0) + 1
    note = (note or "").strip()[:2000]
    parts = recharge_email_parts(req)
    to, cc = reminder_recipients(req, extra_cc)
    cc_text = ", ".join(cc) if cc else "—"
    subject = f"Reminder #{number}: {parts['subject']}"
    banner_text, banner_html = _reminder_banner(req, parts, number, note)
    with_links = req.status == WalletRechargeRequestStatus.PENDING

    approver_messages = []
    for recipient in to if not preview else to[:1] or [""]:
        if with_links and preview:
            approve_url, reject_url = "#approve-link-personal-to-each-recipient", "#decline-link-personal-to-each-recipient"
        elif with_links:
            approve_url, reject_url = build_action_urls(req, approver_email=recipient)
        else:
            approve_url = reject_url = ""
        text, html = approver_email_body(parts, recipient, cc_text, approve_url, reject_url)
        text, html = _with_banner(text, html, banner_text, banner_html, number)
        approver_messages.append({"to": recipient, "text": text, "html": html})

    copy_text, copy_html = approver_email_body(parts, "", cc_text)
    sent_to = ", ".join(to) or "—"
    copy_text = f"Copy for your records. This reminder was sent to the {parts['approver_label']} ({sent_to}).\n\n{copy_text}"
    copy_html = copy_html.replace(
        "<h2>Wallet Recharge Request</h2>",
        "<h2>Wallet Recharge Request</h2>"
        f'<div class="copy-banner">Copy for your records. This reminder was sent to the '
        f"<strong>{escape(parts['approver_label'])}</strong> ({escape(sent_to)}).</div>",
        1,
    )
    copy_text, copy_html = _with_banner(copy_text, copy_html, banner_text, banner_html, number)
    return {
        "reminder_number": number,
        "subject": subject,
        "to": to,
        "cc": cc,
        "includes_action_links": with_links,
        "approver_messages": approver_messages,
        "copy": {"text": copy_text, "html": copy_html},
    }


def send_sric_reminder(req: WalletRechargeRequest, *, actor, note: str = "", extra_cc: Iterable[str] = ()) -> dict:
    """Send Reminder #N to the SRIC approvers (copy to requester + extra CC) and record it. Atomic with the send."""
    with transaction.atomic():
        locked = (
            WalletRechargeRequest.objects.select_for_update(of=("self",))
            .select_related("user", "wallet", "department", "project")
            .get(pk=req.pk)
        )
        blocked = reminder_blocked_reason(locked)
        if blocked:
            raise RechargeAdminActionError(blocked, code="NOT_ELIGIBLE")
        wait = reminder_cooldown_seconds(locked)
        if wait:
            raise RechargeAdminActionError(
                f"A reminder was sent at {_local(locked.sric_reminder_last_sent_at)}. "
                f"Please wait {max(1, (wait + 59) // 60)} more minute(s) before sending another.",
                status=429,
                code="COOLDOWN",
                extra={"cooldown_seconds": wait},
            )
        built = build_sric_reminder(locked, note=note, extra_cc=extra_cc, preview=False)
        if not built["to"]:
            raise RechargeAdminActionError("No SRIC office recipients are configured for this request.")

        messages = []
        for item in built["approver_messages"]:
            message = EmailMultiAlternatives(
                subject=built["subject"],
                body=item["text"],
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[item["to"]],
            )
            message.attach_alternative(item["html"], "text/html")
            messages.append(message)
        get_connection(fail_silently=False).send_messages(messages)
        if built["cc"]:
            try:
                copy = EmailMultiAlternatives(
                    subject=f"{built['subject']} (copy)",
                    body=built["copy"]["text"],
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=built["cc"][:1],
                    cc=built["cc"][1:],
                )
                copy.attach_alternative(built["copy"]["html"], "text/html")
                copy.send(fail_silently=True)
            except Exception:
                logger.exception("Reminder copy failed for WRR-%s", locked.pk)

        now = timezone.now()
        locked.sric_reminder_count = built["reminder_number"]
        locked.sric_reminder_last_sent_at = now
        locked.sric_reminder_last_sent_by = actor if getattr(actor, "pk", None) else None
        locked.save(
            update_fields=[
                "sric_reminder_count",
                "sric_reminder_last_sent_at",
                "sric_reminder_last_sent_by",
                "updated_at",
            ]
        )
        append_audit_log(
            locked,
            action="sric_reminder_sent",
            from_status=locked.status,
            to_status=locked.status,
            actor=actor,
            message=f"Reminder #{built['reminder_number']} sent to {', '.join(built['to'])}"
            + (f"; copy to {', '.join(built['cc'])}" if built["cc"] else "")
            + (f". Note: {note.strip()}" if (note or "").strip() else ""),
            metadata={
                "reminder_number": built["reminder_number"],
                "recipients": built["to"],
                "cc": built["cc"],
                "includes_action_links": built["includes_action_links"],
            },
        )
    return {"request": locked, "reminder_number": built["reminder_number"], "to": built["to"], "cc": built["cc"]}
