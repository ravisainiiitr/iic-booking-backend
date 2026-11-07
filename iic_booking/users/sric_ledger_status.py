"""
SRIC "Ledger Request Status" emails: SRIC tells the faculty member (copy to the portal mailbox) the outcome of a ledger
request. They carry no attachment. An approval that agrees with a credited Wallet_Recharge.csv row settles that row: it is
marked as verified for reconciliation. These emails never move money: anything that does not agree is left for the Main
Administrator, who gets an alert.

Logs carry record ids and counts only; ledger ids, projects and addresses stay in the database and emails.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.users.models.sric_wallet_recharge import (
    SricLedgerStatusResult,
    SricLedgerStatusEmail,
    SricWalletMailMessage,
    SricWalletMailStatus,
    SricWalletRecharge,
    SricWalletRechargeSettings,
    SricWalletRechargeStatus,
)

logger = logging.getLogger(__name__)

SUBJECT_PREFIX = "ledger request status"
APPROVED = "approved"
R = SricLedgerStatusResult
_LABELS = ("Ledger ID", "Amount(Rs)", "Bill Token No.", "Bank Account No", "Bank IFSC Code", "Party Name", "Project No")
PROBLEM_MESSAGES = {
    R.NO_WALLET_CREDIT: "SRIC approved this ledger, but no wallet was credited for it.",
    R.MISMATCH: "SRIC's email differs from the wallet credit.",
    R.NOT_APPROVED: "SRIC reports a status other than Approved for this ledger.",
    R.ORIGIN_UNVERIFIED: "The email's origin could not be verified, so it was not used for reconciliation.",
    R.NOT_CREDITED: "The matching row is not a live wallet credit.",
}


@dataclass
class ParsedLedgerStatus:
    status: str
    ledger_id: str
    direction: str
    amount: Decimal | None
    party_code: str
    project_number: str
    bill_token: str


def subject_of(msg) -> str:
    from iic_booking.users.imap_fetch import _decode_mime_header

    return " ".join((_decode_mime_header(msg.get("Subject")) or "").split())


def is_ledger_status_email(msg) -> bool:
    return subject_of(msg).lower().startswith(SUBJECT_PREFIX)


def _body_text(msg) -> str:
    plain, markup = "", ""
    for part in msg.walk():
        if part.is_multipart() or part.get_filename():
            continue
        decoded = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            markup += decoded
        elif part.get_content_type() == "text/plain":
            plain += decoded
    source = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup or plain)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", source)).split())


def _field(text: str, label: str) -> str:
    following = "|".join(re.escape(x) for x in _LABELS if x != label) + "|Bank credit|Kindly|Warm Regards"
    match = re.search(re.escape(label) + r"\s*:\s*(.*?)\s*(?:(?:" + following + r")\s*:?|$)", text, flags=re.I)
    return match.group(1).strip() if match else ""


def _amount(raw: str) -> tuple[str, Decimal | None]:
    match = re.search(r"\b(Dr|Cr)?\.?\s*(?:Rs\.?)?\s*([\d,]+(?:\.\d+)?)", raw, flags=re.I)
    if not match:
        return "", None
    try:
        return (match.group(1) or "").title(), Decimal(match.group(2).replace(",", ""))
    except InvalidOperation:
        return "", None


def parse(msg) -> ParsedLedgerStatus | None:
    """None when the email names no Ledger ID."""
    text = _body_text(msg)
    ledger_id = _field(text, "Ledger ID").split(" ")[0].upper()
    if not ledger_id:
        return None
    direction, amount = _amount(_field(text, "Amount(Rs)"))
    status = subject_of(msg).split(":", 1)[-1].strip()
    return ParsedLedgerStatus(status=status, ledger_id=ledger_id[:80], direction=direction, amount=amount, party_code=_field(text, "Party Name")[:80],
                              project_number=_field(text, "Project No")[:120], bill_token=_field(text, "Bill Token No.")[:60])


def _same(a: str, b: str) -> bool:
    return (a or "").strip().upper() == (b or "").strip().upper()


def _row_for(ledger_id: str) -> SricWalletRecharge | None:
    return (SricWalletRecharge.objects.filter(ledger_id__iexact=ledger_id, is_test=False).exclude(status=SricWalletRechargeStatus.DUPLICATE)
            .order_by("-created_at").first())


def assess(parsed: ParsedLedgerStatus, *, origin_verified: bool) -> tuple[str, str, SricWalletRecharge | None]:
    """(result, detail, row) for an email. Changes nothing."""
    row = _row_for(parsed.ledger_id)
    if not origin_verified:
        return R.ORIGIN_UNVERIFIED, "", row
    if parsed.status.strip().lower() != APPROVED:
        return R.NOT_APPROVED, "", row
    if row is None:
        return R.NO_WALLET_CREDIT, "", None
    if row.status != SricWalletRechargeStatus.CREDITED or row.reversed_at:
        return R.NOT_CREDITED, "", row
    differs = [name for name, ok in (
        ("direction", parsed.direction == "Dr"),
        ("amount", parsed.amount is not None and row.amount == parsed.amount),
        ("receiver", _same(row.receiver_code, parsed.party_code)),
        ("project", _same(row.project_number, parsed.project_number)),
    ) if not ok]
    if differs:
        return R.MISMATCH, ", ".join(differs), row
    return R.SETTLED, "", row


def _apply(item: SricLedgerStatusEmail, parsed: ParsedLedgerStatus) -> None:
    from iic_booking.users.sric_wallet_recharge import set_verification

    result, detail, row = assess(parsed, origin_verified=item.origin_verified)
    if result == R.SETTLED and not row.fund_receipt_verified:
        when = item.message.received_at.date().isoformat() if item.message.received_at else "unknown date"
        set_verification(row.pk, actor=None, verified=True, remarks=f"SRIC ledger approval email of {when}")
    item.result, item.detail, item.recharge = result, detail, row
    item.save(update_fields=["result", "detail", "recharge", "updated_at"])


def record(message: SricWalletMailMessage, parsed: ParsedLedgerStatus) -> SricLedgerStatusEmail:
    with transaction.atomic():
        item, _ = SricLedgerStatusEmail.objects.get_or_create(message=message, defaults=dict(
            ledger_id=parsed.ledger_id, status_text=parsed.status[:60], direction=parsed.direction[:4], amount=parsed.amount, party_code=parsed.party_code,
            project_number=parsed.project_number, bill_token=parsed.bill_token, origin_verified=message.authenticated))
        _apply(item, parsed)
    return item


def _parsed_from(item: SricLedgerStatusEmail) -> ParsedLedgerStatus:
    return ParsedLedgerStatus(status=item.status_text, ledger_id=item.ledger_id, direction=item.direction, amount=item.amount, party_code=item.party_code,
                              project_number=item.project_number, bill_token=item.bill_token)


def settle_waiting_for_row(row_id: int) -> int:
    """A wallet credit has just been made: an approval that arrived before it can now settle it."""
    row = SricWalletRecharge.objects.filter(pk=row_id).first()
    if row is None:
        return 0
    done = 0
    for item in SricLedgerStatusEmail.objects.filter(ledger_id__iexact=row.ledger_id, result=R.NO_WALLET_CREDIT).select_related("message"):
        with transaction.atomic():
            _apply(item, _parsed_from(item))
        done += int(item.result == R.SETTLED)
    return done


def send_problem_alert(item_ids: list[int]) -> None:
    from iic_booking.communication.styled_transactional_emails import _send
    from iic_booking.communication.utils import get_frontend_absolute_url
    from iic_booking.users.sric_wallet_recharge import review_alert_recipients

    items = list(SricLedgerStatusEmail.objects.filter(pk__in=item_ids).select_related("message"))
    if not items:
        return
    link = get_frontend_absolute_url("/admin-settings/wallet-recharge-requests?tab=sric")
    lines = [f"Ledger ID {i.ledger_id}, amount {i.amount}, email of {i.message.received_at.date() if i.message.received_at else '-'}: "
             f"{PROBLEM_MESSAGES.get(i.result, i.result)}" + (f" ({i.detail})" if i.detail else "") for i in items]
    text = (f"{len(items)} SRIC ledger status email(s) need checking. No wallet was changed.\n\n" + "\n".join(lines) + f"\n\nOpen: {link}\n")
    for to in review_alert_recipients():
        try:
            _send(to, f"SRIC ledger status: {len(items)} email(s) need checking", text, None)
        except Exception:  # noqa: BLE001
            logger.exception("SRIC ledger status alert failed")


def reconcile_mailbox(*, since: date, apply: bool = False) -> dict[str, Any]:
    """Go through status emails already marked "no attachment" since ``since``, and report how the rows stand. The mailbox is
    read-only. Without ``apply`` nothing is stored. With it, running again changes nothing further and no alert is sent."""
    import email

    from django.conf import settings

    from iic_booking.users import sric_wallet_recharge as svc
    from iic_booking.users.imap_fetch import connect_imap

    config = SricWalletRechargeSettings.get_singleton()
    imap = svc._imap_config()
    if imap is None:
        return {"status": "not_configured"}
    folder = (getattr(settings, "IMAP_MAILBOX", "") or "INBOX").strip() or "INBOX"
    out: dict[str, Any] = {"status": "ok", "applied": apply, "emails_checked": 0, "not_status_emails": 0, "unreadable": 0, "results": {}, "rows": []}
    conn = connect_imap(imap["host"], imap["port"], imap["use_ssl"], imap["email_address"], imap["password"], timeout=imap["timeout"])
    try:
        if conn.select(folder, readonly=True)[0] != "OK":
            return {"status": "error"}
        for record_row in SricWalletMailMessage.objects.filter(folder=folder, status=SricWalletMailStatus.NO_ATTACHMENT, received_at__date__gte=since).order_by("pk"):
            raw = svc._fetch(conn, record_row.uid)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw)
            if " ".join(str(msg.get("Message-ID") or "").split())[:500] != record_row.message_id:
                out["not_status_emails"] += 1  # the mailbox has reused this number for a different email
                continue
            out["emails_checked"] += 1
            if not is_ledger_status_email(msg):
                out["not_status_emails"] += 1
                continue
            parsed = parse(msg)
            if parsed is None:
                out["unreadable"] += 1
                continue
            auth = svc.check_message(msg, config)
            if apply:
                with transaction.atomic():
                    record_row.status = SricWalletMailStatus.LEDGER_STATUS
                    record_row.authenticated, record_row.auth_verdict = auth.authenticated, auth.verdict[:500]
                    record_row.save(update_fields=["status", "authenticated", "auth_verdict"])
                    item = record(record_row, parsed)
                result, row_id = item.result, item.recharge_id
            else:
                result, _detail, row = assess(parsed, origin_verified=auth.authenticated)
                row_id = row.pk if row else None
            out["results"][str(result)] = out["results"].get(str(result), 0) + 1
            out["rows"].append({"mail_record": record_row.pk, "row_id": row_id, "result": str(result)})
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    live = SricWalletRecharge.objects.filter(status=SricWalletRechargeStatus.CREDITED, is_test=False, reversed_at__isnull=True)
    out["credited_rows"] = live.count()
    out["credited_rows_verified"] = live.filter(fund_receipt_verified=True).count()
    out["credited_rows_awaiting_sric"] = [r.pk for r in live.filter(fund_receipt_verified=False).order_by("pk")]
    out["at"] = timezone.now().isoformat()
    return out


def overdue_settlements(now=None):
    """Live wallet credits with no settlement after ``WalletSricSettings.fund_receipt_overdue_days``, not alerted in that time."""
    from datetime import timedelta

    from django.db.models import Q

    from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

    now = now or timezone.now()
    cutoff = now - timedelta(days=max(1, WalletSricSettings.get_singleton().fund_receipt_overdue_days))
    return (SricWalletRecharge.objects.filter(status=SricWalletRechargeStatus.CREDITED, is_test=False, reversed_at__isnull=True, fund_receipt_verified=False, credited_at__lte=cutoff)
            .filter(Q(settlement_alert_sent_at__isnull=True) | Q(settlement_alert_sent_at__lte=cutoff)).select_related("matched_user", "department").order_by("credited_at"))


def send_overdue_settlement_alert(now=None) -> dict[str, int]:
    """Email and notify the Accounts In Charge and the Main Administrator. Repeats each overdue period until the row is settled."""
    from iic_booking.communication.in_app import notify_in_app
    from iic_booking.communication.styled_transactional_emails import _send
    from iic_booking.communication.utils import get_frontend_absolute_url
    from iic_booking.users.models import User, UserType
    from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

    now = now or timezone.now()
    rows = list(overdue_settlements(now))
    if not rows:
        return {"overdue": 0, "recipients": 0}
    people = list(User.objects.filter(user_type__in=(UserType.ADMIN, UserType.FINANCE), is_active=True).exclude(email=""))
    days = WalletSricSettings.get_singleton().fund_receipt_overdue_days
    link = get_frontend_absolute_url("/admin-settings/wallet-recharge-requests?tab=sric")
    lines = [f"{r.reference}: Ledger ID {r.ledger_id}, Rs. {r.amount}, project {r.project_number or '-'}, credited {timezone.localtime(r.credited_at):%d %b %Y}" for r in rows]
    subject = f"SRIC wallet recharge: {len(rows)} credit(s) not settled after {days} days"
    text = (f"These wallet credits have no SRIC approval or fund receipt after {days} days. Please check them with the SRIC office and take the "
            f"necessary action.\n\n" + "\n".join(lines) + f"\n\nOpen: {link}\n")
    sent = 0
    for person in people:
        try:
            _send(person.email, subject, text, None)
            sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("SRIC settlement overdue alert failed")
    try:
        notify_in_app(people, title="SRIC credits not settled", message=f"{len(rows)} wallet credit(s) have no settlement after {days} days.", link="/admin-settings/wallet-recharge-requests?tab=sric",
                      notification_type="warning", event="wallet.sric_settlement_overdue", extra={"action_required": True})
    except Exception:  # noqa: BLE001
        logger.debug("SRIC settlement overdue notification skipped", exc_info=True)
    if sent:  # a failed run stays due for tomorrow
        SricWalletRecharge.objects.filter(pk__in=[r.pk for r in rows]).update(settlement_alert_sent_at=now)
    logger.info("SRIC settlement overdue alert: %s row(s), %s recipient(s)", len(rows), sent)
    return {"overdue": len(rows), "recipients": sent}
