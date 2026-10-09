"""
SRIC wallet recharge: faculty transfer money from a project on rnd.iitr.ac.in (Ledger > New Wallet Recharge); the
SRIC portal then emails Wallet_Recharge.csv (Project Number, PI Name, Employee ID, Ledger ID, Receiver Project,
Amount) to the portal mailbox. Each row is stored once per (Ledger ID, financial year), matched to a faculty
member (Employee ID) and a department sub-wallet (Receiver Project mapping), and credited once.

Logs carry record ids and counts only; names, employee ids and ledger ids stay in the database and emails.
"""

from __future__ import annotations

import email
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Iterable

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from iic_booking.users.models import Department, User, UserType
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.sric_wallet_recharge import (
    CREDITABLE_STATUSES,
    SRIC_PORTAL_URL,
    SricReceiverMapping,
    SricWalletMailMessage,
    SricWalletMailStatus,
    SricWalletRecharge,
    SricWalletRechargeSettings,
    SricWalletRechargeStatus,
)
from iic_booking.users.models.wallet import SubWallet, Wallet
from iic_booking.users.sric_wallet_csv import (
    ParsedRow,
    financial_year,
    is_wallet_recharge_attachment,
    normalize_employee_id,
    normalize_receiver_code,
    parse_wallet_recharge_csv,
)
from iic_booking.users.sric_wallet_mail_auth import check_message

logger = logging.getLogger(__name__)

LOCK_KEY = "sric_wallet_recharge_scan_lock"
LOCK_SECONDS = 10 * 60
RECENT_SCAN_KEY = "sric_wallet_recharge_recent_scan"
RECENT_SCAN_SECONDS = 20
USER_REFRESH_SECONDS = 60
MAX_MESSAGES_PER_RUN = 25
DESCRIPTION_PREFIX = "SRIC wallet recharge"
MISSING_LEDGER_PREFIX = "(MISSING)"

S = SricWalletRechargeStatus

REVIEW_MESSAGES = {
    "missing_ledger_id": "The row has no Ledger ID.",
    "missing_employee_id": "The row has no Employee ID.",
    "missing_receiver": "The row has no Receiver Project.",
    "invalid_amount": "The amount is missing, zero, negative or not a number.",
    "unknown_receiver": "The Receiver Project code is not mapped to a department.",
    "receiver_without_department": "The Receiver Project code has no department sub-wallet set.",
    "unmatched_employee": "No portal user has this Employee ID.",
    "ambiguous_employee": "More than one portal user matches this Employee ID.",
    "inactive_user": "The matching user account is inactive.",
    "not_faculty": "The matching user is not a faculty member.",
    "origin_unverified": "The email's origin could not be verified, so it is not credited automatically.",
    "over_auto_limit": "The amount is above the auto-credit limit.",
    "credit_failed": "Crediting the wallet failed.",
}


class SricRechargeError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = 0


# --- Matching ---------------------------------------------------------------------------------------------------


def match_employee(employee_id: str) -> list[User]:
    norm = normalize_employee_id(employee_id)
    if not norm:
        return []
    candidates = User.objects.filter(emp_id__iendswith=norm).only("id", "emp_id", "is_active", "user_type")
    return [u for u in candidates if normalize_employee_id(u.emp_id or "") == norm]


def receiver_mapping(code: str) -> SricReceiverMapping | None:
    code = normalize_receiver_code(code)
    if not code:
        return None
    return SricReceiverMapping.objects.select_related("department").filter(code=code, is_active=True).first()


def _missing_ledger(ledger_id: str) -> bool:
    return not ledger_id or ledger_id.startswith(MISSING_LEDGER_PREFIX)


@dataclass
class RowPlan:
    status: str
    reason: str = ""
    user: User | None = None
    mapping: SricReceiverMapping | None = None
    duplicate_of: SricWalletRecharge | None = None

    def __post_init__(self):
        self.status = str(self.status)


def plan_row(row: ParsedRow, fy: str, *, origin_verified: bool, config: SricWalletRechargeSettings) -> RowPlan:
    if row.ledger_id:
        existing = (
            SricWalletRecharge.objects.filter(ledger_id=row.ledger_id, financial_year=fy)
            .exclude(status=S.DUPLICATE)
            .first()
        )
        if existing:
            return RowPlan(S.DUPLICATE, "duplicate_ledger", duplicate_of=existing)
    mapping = receiver_mapping(row.receiver_code) if row.receiver_code else None
    users = match_employee(row.employee_id) if row.employee_id else []
    user = users[0] if len(users) == 1 else None
    if row.errors:
        return RowPlan(S.NEEDS_REVIEW, row.errors[0], user=user, mapping=mapping)
    if mapping is None:
        return RowPlan(S.NEEDS_REVIEW, "unknown_receiver", user=user)
    if mapping.department_id is None:
        return RowPlan(S.NEEDS_REVIEW, "receiver_without_department", user=user, mapping=mapping)
    if not users:
        return RowPlan(S.NEEDS_REVIEW, "unmatched_employee", mapping=mapping)
    if len(users) > 1:
        return RowPlan(S.NEEDS_REVIEW, "ambiguous_employee", mapping=mapping)
    if not user.is_active:
        return RowPlan(S.NEEDS_REVIEW, "inactive_user", user=user, mapping=mapping)
    if user.user_type != UserType.FACULTY:
        return RowPlan(S.NEEDS_REVIEW, "not_faculty", user=user, mapping=mapping)
    if not origin_verified:
        return RowPlan(S.NEEDS_REVIEW, "origin_unverified", user=user, mapping=mapping)
    limit = config.auto_credit_max_amount
    if limit is not None and row.amount is not None and row.amount > limit:
        return RowPlan(S.NEEDS_REVIEW, "over_auto_limit", user=user, mapping=mapping)
    if not config.auto_credit_enabled:
        return RowPlan(S.AWAITING_CREDIT, "", user=user, mapping=mapping)
    return RowPlan(S.CREDITED, "", user=user, mapping=mapping)


def _history(row: SricWalletRecharge, action: str, actor=None, note: str = "") -> None:
    entry = {"action": action, "at": timezone.now().isoformat()}
    if getattr(actor, "pk", None):
        entry["by"] = actor.pk
    if note:
        entry["note"] = note[:500]
    row.history = list(row.history or []) + [entry]


def store_row(
    row: ParsedRow,
    *,
    message: SricWalletMailMessage | None,
    fy: str,
    origin_verified: bool,
    config,
    is_test: bool = False,
) -> SricWalletRecharge:
    """Store one parsed row (never twice for the same ledger in a financial year) and credit it if eligible.

    Test rows (one-off test-sender run) skip the origin check but are never auto-credited.
    """
    plan = plan_row(row, fy, origin_verified=origin_verified or is_test, config=config)
    ledger_id = row.ledger_id or f"{MISSING_LEDGER_PREFIX}{getattr(message, 'pk', 0)}-{row.row_number}"
    fields = dict(
        message=message,
        row_number=row.row_number,
        project_number=row.project_number,
        pi_name=row.pi_name,
        employee_id=row.employee_id,
        ledger_id=ledger_id,
        receiver_code=row.receiver_code,
        amount_raw=row.amount_raw,
        amount=row.amount,
        financial_year=fy,
        origin_verified=origin_verified,
        matched_user=plan.user,
        receiver_mapping=plan.mapping,
        department=plan.mapping.department if plan.mapping else None,
        is_test=is_test,
    )
    want_credit = plan.status == S.CREDITED and not is_test
    status = S.AWAITING_CREDIT if plan.status == S.CREDITED else plan.status
    try:
        with transaction.atomic():
            rec = SricWalletRecharge(
                status=status,
                review_reason=plan.reason,
                review_message=REVIEW_MESSAGES.get(plan.reason, ""),
                duplicate_of=plan.duplicate_of,
                **fields,
            )
            _history(rec, "received", note=f"status={status}" + (f" reason={plan.reason}" if plan.reason else ""))
            rec.save()
    except IntegrityError:
        original = (
            SricWalletRecharge.objects.filter(ledger_id=ledger_id, financial_year=fy).exclude(status=S.DUPLICATE).first()
        )
        rec = SricWalletRecharge(status=S.DUPLICATE, review_reason="duplicate_ledger", duplicate_of=original, **fields)
        _history(rec, "received", note="status=duplicate reason=duplicate_ledger")
        rec.save()
        want_credit = False
    if rec.status == S.DUPLICATE:
        logger.info("SRIC wallet recharge row %s is a duplicate of row %s", rec.pk, rec.duplicate_of_id)
    if want_credit:
        try:
            rec, _ = credit_row(rec.pk, actor=None, note="auto-credit")
        except SricRechargeError as exc:
            _mark_failed(rec.pk, exc.message)
            rec.refresh_from_db()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SRIC wallet recharge row %s: auto-credit failed (%s)", rec.pk, type(exc).__name__)
            _mark_failed(rec.pk, "Crediting the wallet failed.")
            rec.refresh_from_db()
    return rec


def _mark_failed(row_id: int, message: str) -> None:
    with transaction.atomic():
        rec = SricWalletRecharge.objects.select_for_update().get(pk=row_id)
        if rec.status == S.CREDITED:
            return
        rec.status = S.FAILED
        rec.review_reason = "credit_failed"
        rec.review_message = message[:500]
        _history(rec, "credit_failed", note=message)
        rec.save()


# --- Crediting --------------------------------------------------------------------------------------------------


def credit_key(rec: SricWalletRecharge) -> str:
    return f"{rec.ledger_id}|{rec.financial_year}"


def credit_description(rec: SricWalletRecharge) -> str:
    receiver = rec.receiver_mapping.label if rec.receiver_mapping else rec.receiver_code
    text = f"{DESCRIPTION_PREFIX} {rec.reference} — Ledger ID {rec.ledger_id}"
    if rec.project_number:
        text += f", Project {rec.project_number}"
    if receiver:
        text += f" ({receiver})"
    return text


def credit_row(
    row_id: int,
    *,
    actor=None,
    user: User | None = None,
    department: Department | None = None,
    mapping: SricReceiverMapping | None = None,
    note: str = "",
) -> tuple[SricWalletRecharge, bool]:
    """Credit one row exactly once. Returns ``(row, credited_now)``; a row already credited is returned as is."""
    with transaction.atomic():
        rec = (
            SricWalletRecharge.objects.select_for_update(of=("self",))
            .select_related("receiver_mapping", "matched_user", "department")
            .get(pk=row_id)
        )
        if rec.status == S.CREDITED or rec.wallet_transaction_id:
            return rec, False
        if rec.status not in CREDITABLE_STATUSES:
            raise SricRechargeError(f"A {rec.get_status_display().lower()} row cannot be credited.", status=409, code="NOT_CREDITABLE")
        if _missing_ledger(rec.ledger_id):
            raise SricRechargeError("A row without a Ledger ID cannot be credited; reject it.", code="NO_LEDGER")
        if rec.amount is None or rec.amount <= 0:
            raise SricRechargeError("The amount must be more than zero.", code="INVALID_AMOUNT")
        if mapping is not None:
            rec.receiver_mapping = mapping
            department = department or mapping.department
        target_user = user or rec.matched_user
        target_dept = department or rec.department or (rec.receiver_mapping.department if rec.receiver_mapping else None)
        if target_user is None:
            raise SricRechargeError("Select the faculty member to credit.", code="USER_REQUIRED")
        if target_dept is None or target_dept.department_type != DepartmentType.INTERNAL:
            raise SricRechargeError("Select the receiver (department sub-wallet) to credit.", code="DEPARTMENT_REQUIRED")
        if not target_user.is_active or not target_user.can_have_wallet():
            raise SricRechargeError("This user cannot hold a wallet.", code="USER_NOT_ELIGIBLE")
        key = credit_key(rec)
        if SricWalletRecharge.objects.filter(credit_key=key).exclude(pk=rec.pk).exists():
            raise SricRechargeError("This Ledger ID was already credited this financial year.", status=409, code="DUPLICATE")

        wallet = Wallet.objects.filter(user=target_user).first()
        if wallet is None:
            wallet = Wallet(user=target_user)
            wallet.save()
        sub, _ = SubWallet.objects.get_or_create(wallet=wallet, department=target_dept, defaults={"balance": Decimal("0.00")})
        sub = SubWallet.objects.select_for_update().get(pk=sub.pk)
        rec.matched_user = target_user
        rec.department = target_dept
        txn = sub.credit(rec.amount, credit_description(rec), related_user=target_user)
        sub.refresh_from_db(fields=["balance"])
        rec.sub_wallet = sub
        rec.wallet_transaction = txn
        rec.credit_key = key
        rec.balance_after = sub.balance
        rec.status = S.CREDITED
        rec.review_reason = ""
        rec.review_message = ""
        rec.credited_at = timezone.now()
        rec.credited_by = actor if getattr(actor, "pk", None) else None
        _history(rec, "credited", actor, note)
        try:
            with transaction.atomic():
                rec.save()
        except IntegrityError as exc:
            raise SricRechargeError("This Ledger ID was already credited this financial year.", status=409, code="DUPLICATE") from exc
        row_pk = rec.pk
        transaction.on_commit(lambda: send_credit_confirmation(row_pk))
    logger.info("SRIC wallet recharge row %s credited to sub-wallet %s (txn %s)", rec.pk, sub.pk, txn.pk)
    return rec, True


def reject_row(row_id: int, *, actor, reason: str) -> SricWalletRecharge:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise SricRechargeError("Enter the reason for rejecting this row.", code="REASON_REQUIRED")
    with transaction.atomic():
        rec = SricWalletRecharge.objects.select_for_update().get(pk=row_id)
        if rec.status not in CREDITABLE_STATUSES:
            raise SricRechargeError(f"A {rec.get_status_display().lower()} row cannot be rejected.", status=409, code="NOT_REJECTABLE")
        rec.status = S.REJECTED
        rec.rejected_by = actor if getattr(actor, "pk", None) else None
        rec.rejected_at = timezone.now()
        rec.rejection_reason = reason[:2000]
        _history(rec, "rejected", actor, reason)
        rec.save()
    return rec


def set_verification(row_id: int, *, actor, verified: bool, remarks: str = "") -> SricWalletRecharge:
    with transaction.atomic():
        rec = SricWalletRecharge.objects.select_for_update().get(pk=row_id)
        if rec.status != S.CREDITED:
            raise SricRechargeError("Only credited rows can be checked against the fund receipt.", code="NOT_CREDITED")
        if verified and rec.reversed_at:
            raise SricRechargeError("This credit was reversed; it cannot be marked as verified.", code="REVERSED")
        rec.fund_receipt_verified = bool(verified)
        rec.fund_receipt_verified_by = actor if getattr(actor, "pk", None) else None
        rec.fund_receipt_verified_at = timezone.now()
        rec.fund_receipt_verification_remarks = (remarks or "").strip()[:2000]
        _history(rec, "fund_receipt_verified" if verified else "fund_receipt_not_verified", actor, remarks)
        rec.save()
    return rec


# --- Emails -----------------------------------------------------------------------------------------------------


def _addresses(raw: str) -> list[str]:
    import re

    seen: list[str] = []
    for item in re.split(r"[\s,;]+", raw or ""):
        item = item.strip()
        if item and "@" in item and item.lower() not in {s.lower() for s in seen}:
            seen.append(item)
    return seen


def receiver_label(rec: SricWalletRecharge) -> str:
    label = rec.receiver_mapping.label if rec.receiver_mapping else ""
    return label or rec.receiver_code or "—"


def send_credit_confirmation(row_id: int) -> None:
    from html import escape

    from iic_booking.communication.email_branding import format_email_datetime, format_inr, wrap_email_html
    from iic_booking.communication.styled_transactional_emails import _send
    from iic_booking.communication.utils import get_frontend_absolute_url
    from iic_booking.users.display import get_user_display_name

    try:
        rec = SricWalletRecharge.objects.select_related("matched_user", "department", "receiver_mapping").get(pk=row_id)
    except SricWalletRecharge.DoesNotExist:
        return
    owner = rec.matched_user
    if rec.status != S.CREDITED or owner is None or rec.confirmation_sent_at:
        return
    amount = format_inr(rec.amount) or f"₹{rec.amount:,.2f}"
    dept = rec.department.name if rec.department else "—"
    headline = f"{amount} credited to your {dept} wallet"
    rows = [
        ("Reference", rec.reference),
        ("Amount", amount),
        ("SRIC Ledger ID", rec.ledger_id),
        ("Project Number", rec.project_number or "—"),
        ("Receiver", receiver_label(rec)),
        ("Department sub-wallet", dept),
        ("New balance", format_inr(rec.balance_after) or "—"),
        ("Date", format_email_datetime(rec.credited_at)),
    ]
    link = get_frontend_absolute_url("/wallet")
    name = get_user_display_name(owner)
    intro = (
        "Your wallet recharge from the SRIC portal (rnd.iitr.ac.in) has been credited to your IIC booking wallet. "
        "The entry appears in your wallet transactions."
    )
    if rec.is_test:
        intro += " This credit was made while testing the new SRIC recharge process."
    text = f"Dear {name},\n\n{intro}\n\n" + "\n".join(f"{k}: {v}" for k, v in rows) + f"\n\nView your wallet: {link}\n"
    table = "".join(
        f"<tr><td style='padding:6px 12px 6px 0;color:#475569;'><strong>{escape(k)}</strong></td>"
        f"<td style='padding:6px 0;color:#0f172a;'>{escape(str(v))}</td></tr>"
        for k, v in rows
    )
    body = (
        f"<p>Dear {escape(name)},</p><p>{escape(intro)}</p>"
        f"<table cellpadding='0' cellspacing='0' style='font-family:Arial,Helvetica,sans-serif;font-size:14px;'>{table}</table>"
        f"<p style='margin-top:18px;'><a href='{escape(link)}'>View your wallet</a></p>"
    )
    subject = f"{'[TEST] ' if rec.is_test else ''}[{rec.reference}] {headline}"
    html = wrap_email_html(title=headline, subtitle=rec.reference, body_inner_html=body)
    try:
        if owner.email:
            _send(owner.email, subject, text, html)
            SricWalletRecharge.objects.filter(pk=rec.pk).update(confirmation_sent_at=timezone.now())
        for cc in [] if rec.is_test else _addresses(SricWalletRechargeSettings.get_singleton().confirmation_cc_emails):
            _send(cc, f"{subject} (copy)", text, html)
    except Exception:  # noqa: BLE001
        logger.exception("SRIC wallet recharge confirmation email failed for row %s", rec.pk)
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [owner],
            title="Wallet credited",
            message=f"{headline} (SRIC Ledger ID {rec.ledger_id}).",
            link="/wallet",
            notification_type="success",
            event="wallet.sric_recharge_credited",
            extra={"sric_wallet_recharge_id": rec.pk},
        )
    except Exception:  # noqa: BLE001
        logger.debug("SRIC recharge in-app notification skipped", exc_info=True)


def review_alert_recipients() -> list[str]:
    configured = _addresses(SricWalletRechargeSettings.get_singleton().review_alert_emails)
    if configured:
        return configured
    admins = User.objects.filter(user_type=UserType.ADMIN, is_active=True).exclude(email="").values_list("email", flat=True)
    return sorted({e for e in admins if e})


def send_review_alert(row_ids: Iterable[int]) -> None:
    from html import escape

    from iic_booking.communication.email_branding import format_inr, wrap_email_html
    from iic_booking.communication.styled_transactional_emails import _send
    from iic_booking.communication.utils import get_frontend_absolute_url

    rows = list(SricWalletRecharge.objects.filter(pk__in=list(row_ids)).order_by("pk"))
    if not rows:
        return
    recipients = review_alert_recipients()
    link = get_frontend_absolute_url("/admin-settings/wallet-recharge-requests?tab=sric")
    lines = [
        (r.reference, r.get_status_display(), REVIEW_MESSAGES.get(r.review_reason, r.review_reason or "—"),
         r.employee_id or "—", r.ledger_id, r.receiver_code or "—", format_inr(r.amount) or r.amount_raw or "—")
        for r in rows
    ]
    heads = ("Reference", "Status", "Reason", "Employee ID", "Ledger ID", "Receiver", "Amount")
    intro = f"{len(rows)} SRIC wallet recharge row(s) need your action before any wallet is credited."
    text = intro + "\n\n" + "\n".join(" | ".join(map(str, line)) for line in lines) + f"\n\nOpen: {link}\n"
    table = "<tr>" + "".join(f"<th style='text-align:left;padding:4px 8px;'>{h}</th>" for h in heads) + "</tr>" + "".join(
        "<tr>" + "".join(f"<td style='padding:4px 8px;border-top:1px solid #e2e8f0;'>{escape(str(c))}</td>" for c in line) + "</tr>"
        for line in lines
    )
    body = (
        f"<p>{escape(intro)}</p><table cellpadding='0' cellspacing='0' style='font-size:13px;'>{table}</table>"
        f"<p style='margin-top:16px;'><a href='{escape(link)}'>Open SRIC recharges</a></p>"
    )
    html = wrap_email_html(title="SRIC wallet recharges need review", subtitle=f"{len(rows)} row(s)", body_inner_html=body)
    for to in recipients:
        try:
            _send(to, f"SRIC wallet recharge: {len(rows)} row(s) need review", text, html)
        except Exception:  # noqa: BLE001
            logger.exception("SRIC wallet recharge review alert failed")


# --- Mailbox ----------------------------------------------------------------------------------------------------


def _cutoff_date():
    from iic_booking.users.models.wallet_sric_settings import cashbook_match_from_date

    return cashbook_match_from_date()


def _message_date(msg) -> datetime:
    try:
        dt = parsedate_to_datetime(msg.get("Date") or "")
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        return timezone.now()
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _attachments(msg, expected: str) -> list[tuple[str, bytes]]:
    from iic_booking.users.imap_fetch import _decode_mime_header

    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = _decode_mime_header(part.get_filename()) or ""
        if filename and is_wallet_recharge_attachment(filename, expected):
            out.append((filename, part.get_payload(decode=True) or b""))
    return out


def process_message(
    raw: bytes, *, uid: str, folder: str, config: SricWalletRechargeSettings, dry_run: bool = False, trigger: str = ""
) -> dict[str, Any]:
    """Handle one email. Returns a summary with counts and record ids only."""
    msg = email.message_from_bytes(raw)
    message_id = " ".join(str(msg.get("Message-ID") or "").split())[:500]
    received_at = _message_date(msg)
    from_addr = parseaddr(msg.get("From") or "")[1].lower()[:255]
    record = dict(folder=folder, uid=uid, message_id=message_id, received_at=received_at, from_addr=from_addr, trigger=trigger[:40])
    summary: dict[str, Any] = {"uid": uid, "date": received_at.date().isoformat(), "rows": 0, "statuses": {}, "reasons": {}}

    def finish(status: str, **extra) -> dict[str, Any]:
        summary["status"] = str(status)
        if not dry_run:
            obj = SricWalletMailMessage.objects.create(status=status, **record, **extra)
            summary["message_record_id"] = obj.pk
        return summary

    if from_addr != (config.sender_email or "").strip().lower():
        return finish(SricWalletMailStatus.WRONG_SENDER)
    if received_at.date() < _cutoff_date():
        return finish(SricWalletMailStatus.BEFORE_CUTOFF)
    if message_id and SricWalletMailMessage.objects.filter(message_id=message_id).exclude(folder=folder, uid=uid).exists():
        return finish(SricWalletMailStatus.DUPLICATE_MESSAGE)
    files = _attachments(msg, config.attachment_name)
    if not files:
        return finish(SricWalletMailStatus.NO_ATTACHMENT)
    filename, content = files[0]
    sha = hashlib.sha256(content).hexdigest()
    auth = check_message(msg, config)
    summary["authenticated"] = auth.authenticated
    summary["auth_verdict"] = auth.verdict
    parsed = parse_wallet_recharge_csv(content)
    extra = dict(
        attachment_name=filename[:255],
        attachment_sha256=sha,
        row_count=len(parsed.rows),
        authenticated=auth.authenticated,
        auth_verdict=auth.verdict[:500],
    )
    if parsed.error:
        summary["error"] = parsed.error
        return finish(SricWalletMailStatus.PARSE_ERROR, error=parsed.error, **extra)
    fy = financial_year(timezone.localtime(received_at))
    summary["financial_year"] = fy
    summary["rows"] = len(parsed.rows)
    statuses: dict[str, int] = {}
    reasons: dict[str, int] = {}
    if dry_run:
        for row in parsed.rows:
            plan = plan_row(row, fy, origin_verified=auth.authenticated, config=config)
            statuses[plan.status] = statuses.get(plan.status, 0) + 1
            if plan.reason:
                reasons[plan.reason] = reasons.get(plan.reason, 0) + 1
        summary.update(statuses=statuses, reasons=reasons, status=str(SricWalletMailStatus.DRY_RUN))
        return summary
    with transaction.atomic():
        obj = SricWalletMailMessage.objects.create(status=SricWalletMailStatus.PROCESSED, **record, **extra)
    summary["message_record_id"] = obj.pk
    row_ids: list[int] = []
    review_ids: list[int] = []
    for row in parsed.rows:
        rec = store_row(row, message=obj, fy=fy, origin_verified=auth.authenticated, config=config)
        row_ids.append(rec.pk)
        statuses[str(rec.status)] = statuses.get(str(rec.status), 0) + 1
        if rec.review_reason:
            reasons[rec.review_reason] = reasons.get(rec.review_reason, 0) + 1
        if rec.status in (S.NEEDS_REVIEW, S.FAILED):
            review_ids.append(rec.pk)
    summary.update(
        statuses=statuses, reasons=reasons, row_ids=row_ids, review_row_ids=review_ids, status=str(SricWalletMailStatus.PROCESSED)
    )
    return summary


def _imap_config():
    from iic_booking.users.wallet_cashbook_mailbox import _imap_config as cashbook_imap_config

    return cashbook_imap_config()


def _search_uids(conn, sender: str, since) -> list[str]:
    safe = sender.replace("\\", "\\\\").replace('"', '\\"')
    criteria = f'FROM "{safe}" SINCE {since.strftime("%d-%b-%Y")}'
    typ, data = conn.uid("search", None, criteria)
    if typ != "OK":
        raise RuntimeError("IMAP search failed")
    return [u.decode() if isinstance(u, bytes) else str(u) for u in (data[0].split() if data and data[0] else [])]


def _fetch(conn, uid: str) -> bytes | None:
    typ, data = conn.uid("fetch", uid.encode(), "(BODY.PEEK[])")
    if typ != "OK" or not data:
        return None
    return next((p[1] for p in data if isinstance(p, tuple) and len(p) >= 2), None)


QUIET_WINDOW_TZ = "Asia/Kolkata"
QUIET_WINDOW_TRIGGERS = ("schedule", "refresh")
SKIPPED_PEAK = "skipped_peak_window"


def in_quiet_window(config: SricWalletRechargeSettings, now: datetime | None = None) -> bool:
    """True inside the weekly peak-booking pause (IST): start inclusive, end exclusive."""
    from zoneinfo import ZoneInfo

    start, end = config.quiet_window_start, config.quiet_window_end
    if not config.quiet_window_enabled or start is None or end is None or start >= end:
        return False
    local = timezone.localtime(now or timezone.now(), ZoneInfo(QUIET_WINDOW_TZ))
    return local.weekday() == config.quiet_window_weekday and start <= local.time() < end


def _clock(t) -> str:
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d}"


def quiet_window_label(config: SricWalletRechargeSettings) -> dict[str, str]:
    """Human labels, e.g. {"window": "Wednesday 8:55–9:15 PM", "resume": "9:15 PM"}."""
    from iic_booking.users.models.sric_wallet_recharge import WEEKDAY_CHOICES

    start, end = config.quiet_window_start, config.quiet_window_end
    day = str(dict(WEEKDAY_CHOICES).get(config.quiet_window_weekday, ""))
    s_ampm, e_ampm = ("AM" if start.hour < 12 else "PM"), ("AM" if end.hour < 12 else "PM")
    span = f"{_clock(start)}–{_clock(end)} {e_ampm}" if s_ampm == e_ampm else f"{_clock(start)} {s_ampm}–{_clock(end)} {e_ampm}"
    return {"window": f"{day} {span}", "resume": f"{_clock(end)} {e_ampm}"}


def paused_message(config: SricWalletRechargeSettings) -> str:
    label = quiet_window_label(config)
    return (
        f"Recharge checks are paused during peak booking time ({label['window']}). "
        f"Your recharge will be credited automatically after {label['resume']}."
    )


def scan_mailbox(*, trigger: str = "schedule", dry_run: bool = False, max_messages: int = MAX_MESSAGES_PER_RUN) -> dict[str, Any]:
    """Read new SRIC wallet recharge emails (read-only IMAP; nothing is moved, flagged or deleted)."""
    config = SricWalletRechargeSettings.get_singleton()
    if not dry_run and not config.scan_enabled:
        return {"status": "disabled"}
    if not dry_run and trigger in QUIET_WINDOW_TRIGGERS and in_quiet_window(config):
        logger.info("SRIC wallet recharge scan (%s) skipped: peak window", trigger)
        if trigger == "schedule":
            result = {**(config.last_scan_result or {}), "status": SKIPPED_PEAK, "skipped_at": timezone.now().isoformat()}
            SricWalletRechargeSettings.objects.filter(pk=config.pk).update(last_scan_result=result)
        return {"status": SKIPPED_PEAK, "trigger": trigger}
    imap = _imap_config()
    if imap is None:
        return {"status": "not_configured"}
    if not dry_run and not cache.add(LOCK_KEY, "1", LOCK_SECONDS):
        return {"status": "already_running"}
    try:
        result = _scan(config, imap, trigger=trigger, dry_run=dry_run, max_messages=max_messages)
    finally:
        if not dry_run:
            cache.delete(LOCK_KEY)
    if not dry_run:
        SricWalletRechargeSettings.objects.filter(pk=config.pk).update(
            last_scan_at=timezone.now(), last_scan_result={k: v for k, v in result.items() if k != "messages"}
        )
        if result.get("review_row_ids"):
            send_review_alert(result["review_row_ids"])
    return result


def _scan(config, imap: dict, *, trigger: str, dry_run: bool, max_messages: int) -> dict[str, Any]:
    from django.conf import settings as dj_settings

    from iic_booking.users.imap_fetch import connect_imap

    folder = (getattr(dj_settings, "IMAP_MAILBOX", "") or "INBOX").strip() or "INBOX"
    sender = (config.sender_email or "").strip()
    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "trigger": trigger,
        "messages_found": 0,
        "messages_new": 0,
        "messages_read": 0,
        "rows_new": 0,
        "credited": 0,
        "statuses": {},
        "review_row_ids": [],
        "messages": [],
    }
    try:
        conn = connect_imap(
            imap["host"], imap["port"], imap["use_ssl"], imap["email_address"], imap["password"], timeout=imap["timeout"]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("SRIC wallet recharge scan: IMAP connection failed (%s)", type(exc).__name__)
        return {**result, "status": "error", "error": "Could not connect to the mailbox."}
    try:
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            return {**result, "status": "error", "error": "Could not open the mailbox folder."}
        uids = _search_uids(conn, sender, _cutoff_date())
        result["messages_found"] = len(uids)
        seen = set(SricWalletMailMessage.objects.filter(folder=folder).values_list("uid", flat=True))
        new = [u for u in uids if u not in seen]
        result["messages_new"] = len(new)
        for uid in new[:max_messages]:
            raw = _fetch(conn, uid)
            if raw is None:
                continue
            try:
                summary = process_message(raw, uid=uid, folder=folder, config=config, dry_run=dry_run, trigger=trigger)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SRIC wallet recharge scan: message uid %s failed (%s)", uid, type(exc).__name__)
                if not dry_run:
                    SricWalletMailMessage.objects.get_or_create(
                        folder=folder, uid=uid, defaults={"status": SricWalletMailStatus.ERROR, "error": type(exc).__name__, "trigger": trigger[:40]}
                    )
                continue
            result["messages_read"] += 1
            result["rows_new"] += summary.get("rows", 0) if summary.get("status") in ("processed", "dry_run") else 0
            for status, n in summary.get("statuses", {}).items():
                result["statuses"][status] = result["statuses"].get(status, 0) + n
            result["credited"] += summary.get("statuses", {}).get(S.CREDITED, 0) if not dry_run else 0
            result["review_row_ids"] += summary.get("review_row_ids", [])
            result["messages"].append({k: v for k, v in summary.items() if k not in ("row_ids", "review_row_ids")})
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    logger.info(
        "SRIC wallet recharge scan (%s%s): found=%s new=%s read=%s rows=%s statuses=%s",
        trigger,
        ", dry run" if dry_run else "",
        result["messages_found"],
        result["messages_new"],
        result["messages_read"],
        result["rows_new"],
        result["statuses"],
    )
    return result


# --- Refresh (on demand) ----------------------------------------------------------------------------------------


def refresh(user, *, per_user_seconds: int = USER_REFRESH_SECONDS, trigger: str = "refresh") -> dict[str, Any]:
    """Refresh button: rate-limited per user, debounced globally. Returns the scan status and the user's new rows."""
    if trigger == "refresh":
        config = SricWalletRechargeSettings.get_singleton()
        if config.scan_enabled and in_quiet_window(config):
            return {"scan": {"status": SKIPPED_PEAK, "trigger": trigger}, "rows": [], "message": paused_message(config)}
    key = f"sric_wallet_refresh_user:{user.pk}"
    now = time.time()
    if not cache.add(key, now + per_user_seconds, per_user_seconds):
        wait = max(1, int((cache.get(key) or now + per_user_seconds) - now))
        err = SricRechargeError(f"Please wait {wait} seconds before refreshing again.", status=429, code="RATE_LIMITED")
        err.retry_after = wait
        raise err
    started = timezone.now() - timedelta(seconds=2)
    recent = cache.get(RECENT_SCAN_KEY)
    if recent:
        scan = {**recent, "debounced": True}
    else:
        scan = scan_mailbox(trigger=trigger, max_messages=10)
        if scan.get("status") in ("ok", "disabled", "not_configured"):
            cache.set(RECENT_SCAN_KEY, {k: v for k, v in scan.items() if k not in ("messages", "review_row_ids")}, RECENT_SCAN_SECONDS)
    mine = list(
        SricWalletRecharge.objects.filter(matched_user=user, updated_at__gte=started)
        .select_related("receiver_mapping", "department")
        .order_by("pk")
    )
    return {"scan": {k: v for k, v in scan.items() if k not in ("messages", "review_row_ids")}, "rows": mine}


def portal_info() -> dict[str, Any]:
    config = SricWalletRechargeSettings.get_singleton()
    receivers = [
        {"code": m.code, "label": m.label, "department_name": m.department.name if m.department else ""}
        for m in SricReceiverMapping.objects.select_related("department").filter(is_active=True)
    ]
    return {
        "portal_url": SRIC_PORTAL_URL,
        "scan_enabled": config.scan_enabled,
        "auto_credit_enabled": config.auto_credit_enabled,
        "last_scan_at": config.last_scan_at.isoformat() if config.last_scan_at else None,
        "receivers": receivers,
    }
