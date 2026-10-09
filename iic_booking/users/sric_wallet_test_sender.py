"""One-off end-to-end test of the SRIC Wallet_Recharge.csv pipeline with an email from a test sender.

Run-scoped only: the configured sender, origin checks and auto-credit setting are never changed.

* ``dry_run``: find emails from the test sender (scanner folder + Junk/Spam) and report what each CSV row would
  get. Nothing is stored or credited.
* ``e2e``: take the one email named by folder + IMAP UID and push its rows through the real path for an existing
  flagged test faculty account: the Employee ID (and PI name) are substituted for this run only, the Ledger ID is
  stored as ``TEST-<ledger>`` (never colliding with a real SRIC ledger), the origin check is bypassed for this email
  alone, and everything stored is flagged ``is_test``. Running it again feeds the same rows once more to prove the
  duplicate guard (no second credit). The real faculty member in the file is never matched, credited or emailed.
* ``reverse``: debit a credited test row with a Main Administrator ledger adjustment ('SRIC CSV test reversal').

Mailbox access is read-only (SELECT readonly, BODY.PEEK).
"""

from __future__ import annotations

import email
import hashlib
import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from email.utils import parseaddr
from typing import Any

from django.db.models import Q
from django.utils import timezone

from iic_booking.users import sric_wallet_recharge as svc
from iic_booking.users.models import SubWallet, User, UserType
from iic_booking.users.models.sric_wallet_recharge import (
    SricWalletMailMessage,
    SricWalletMailStatus,
    SricWalletRecharge,
    SricWalletRechargeSettings,
    SricWalletRechargeStatus,
)
from iic_booking.users.sric_wallet_csv import ParsedRow, financial_year, parse_wallet_recharge_csv
from iic_booking.users.sric_wallet_mail_auth import check_message

S = SricWalletRechargeStatus
TEST_LEDGER_PREFIX = "TEST-"
TEST_TRIGGER = "test-sender"
TEST_PI_NAME = "Test faculty (substituted for SRIC CSV test)"
REVERSAL_REMARKS = "SRIC CSV test reversal"
LOOKBACK_DAYS = 60
MAX_MESSAGES = 20
EMAIL_RE = re.compile(r"^[^@\s\"]+@[^@\s\"]+\.[a-z]{2,}$", re.I)
LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')


class TestSenderError(Exception):
    pass


def mask(address: str) -> str:
    local, _, domain = (address or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _sender(test_sender: str, config: SricWalletRechargeSettings) -> str:
    sender = (test_sender or "").strip().lower()
    if not EMAIL_RE.match(sender):
        raise TestSenderError("Give the test sender as an email address.")
    if sender == (config.sender_email or "").strip().lower():
        raise TestSenderError("The test sender must differ from the configured SRIC sender; use the normal scan.")
    return sender


def _folder_names(conn, primary: str) -> list[str]:
    names = [primary]
    try:
        typ, data = conn.list()
    except Exception:  # noqa: BLE001
        return names
    if typ != "OK":
        return names
    for line in data or []:
        text = line.decode(errors="replace") if isinstance(line, bytes) else str(line or "")
        m = LIST_RE.match(text.strip())
        if not m:
            continue
        name = m.group("name").strip().strip('"')
        if name not in names and re.search(r"junk|spam", name, re.I):
            names.append(name)
    return names


def _quote(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9._/-]+", name) else '"' + name.replace('"', '\\"') + '"'


def test_ledger(ledger_id: str) -> str:
    return f"{TEST_LEDGER_PREFIX}{ledger_id}" if ledger_id else ""


def _match_detail(employee_id: str) -> str:
    users = svc.match_employee(employee_id) if employee_id else []
    if not users:
        return "none"
    if len(users) > 1:
        return "multiple"
    user = users[0]
    if not user.is_active:
        return "inactive"
    if user.user_type != UserType.FACULTY:
        return "not_faculty"
    return "one_active_faculty"


def _exists(ledger_id: str, fy: str) -> bool:
    return bool(ledger_id) and SricWalletRecharge.objects.filter(ledger_id=ledger_id, financial_year=fy).exclude(
        status=S.DUPLICATE
    ).exists()


def plan_test_row(row: ParsedRow, fy: str, config: SricWalletRechargeSettings) -> dict[str, Any]:
    """What this row would get (codes, amounts and ids only; no names or Employee IDs). Nothing is stored."""
    real = svc.plan_row(row, fy, origin_verified=True, config=config)
    mapping = svc.receiver_mapping(row.receiver_code) if row.receiver_code else None
    detail = _match_detail(row.employee_id)
    would_be = S.AWAITING_CREDIT if real.status == S.CREDITED else real.status
    return {
        "row": row.row_number,
        "parse_ok": not row.errors,
        "errors": list(row.errors),
        "employee_matched": detail == "one_active_faculty",
        "match_detail": detail,
        "receiver_code": row.receiver_code,
        "receiver_label": mapping.label if mapping else "",
        "department_id": mapping.department_id if mapping else None,
        "department": mapping.department.name if mapping and mapping.department else "",
        "amount": f"{row.amount:.2f}" if row.amount is not None else None,
        "ledger_id": row.ledger_id,
        "financial_year": fy,
        "duplicate_real_ledger": _exists(row.ledger_id, fy),
        "would_be": str(would_be),
        "reason": real.reason,
    }


def eligible_test_faculty(user: User | None) -> tuple[bool, str]:
    from iic_booking.users.test_accounts import is_test_user

    if user is None:
        return False, "not_found"
    if not is_test_user(user):
        return False, "not_a_flagged_test_account"
    if not user.is_active or user.user_type != UserType.FACULTY:
        return False, "not_an_active_faculty"
    if not (user.emp_id or "").strip():
        return False, "no_employee_id"
    if [u.pk for u in svc.match_employee(user.emp_id)] != [user.pk]:
        return False, "employee_id_not_unique"
    return True, "ok"


def test_faculty_candidates() -> list[dict[str, Any]]:
    """Existing flagged test faculty accounts (ids, flags and balances only)."""
    from iic_booking.users.test_accounts import FORCE_EMAIL_REDIRECT_ADDRESSES

    out = []
    iic = svc.receiver_mapping("IIC-000-002")
    flagged = Q(is_test_account=True) | Q(email__in=sorted(FORCE_EMAIL_REDIRECT_ADDRESSES))
    for user in User.objects.filter(flagged, user_type=UserType.FACULTY).order_by("pk"):
        ok, why = eligible_test_faculty(user)
        sub = SubWallet.objects.filter(wallet__user=user, department_id=iic.department_id).first() if iic else None
        out.append({"user_id": user.pk, "eligible": ok, "detail": why, "has_employee_id": bool((user.emp_id or "").strip()),
                    "iic_sub_wallet": sub is not None, "iic_balance": f"{sub.balance:.2f}" if sub else None})
    return out


def _main_admin(actor_id: int | None) -> User:
    qs = User.objects.filter(is_active=True, is_test_account=False).filter(Q(is_superuser=True) | Q(user_type=UserType.ADMIN))
    actor = qs.filter(pk=actor_id).first() if actor_id else qs.order_by("-is_superuser", "pk").first()
    if actor is None:
        raise TestSenderError("No active Main Administrator account to act as.")
    return actor


def _balance(user: User, department_id: int | None) -> Decimal | None:
    sub = SubWallet.objects.filter(wallet__user=user, department_id=department_id).first()
    return sub.balance if sub else None


def _visible(row: SricWalletRecharge, test_user: User, actor: User) -> dict[str, bool]:
    from rest_framework.test import APIRequestFactory, force_authenticate

    from iic_booking.users.api import sric_wallet_recharge_views as views

    factory = APIRequestFactory()
    req = factory.get("/api/admin/sric-wallet-recharges/", {"search": row.ledger_id, "page_size": 50})
    force_authenticate(req, user=actor)
    admin_rows = [r for r in views.admin_list(req).data.get("results", []) if r["id"] == row.pk]
    req = factory.get("/api/wallet/sric-recharges/")
    force_authenticate(req, user=test_user)
    mine = [r for r in views.my_sric_recharges(req).data.get("results", []) if r["id"] == row.pk]
    return {
        "in_admin_tab": bool(admin_rows),
        "admin_row_is_test": bool(admin_rows and admin_rows[0].get("is_test")),
        "on_faculty_page": bool(mine),
    }


def _e2e(msg, content: bytes, filename: str, *, folder: str, uid: str, auth, config, test_user: User, actor: User) -> dict[str, Any]:
    parsed = parse_wallet_recharge_csv(content)
    if parsed.error:
        raise TestSenderError(f"The test CSV could not be read: {parsed.error}")
    if not parsed.rows:
        raise TestSenderError("The test CSV has no rows.")
    dept_ids = {}
    for row in parsed.rows:
        mapping = svc.receiver_mapping(row.receiver_code) if row.receiver_code else None
        if mapping is None or mapping.department_id is None:
            raise TestSenderError(f"Row {row.row_number}: the receiver is not mapped to a department; nothing was stored.")
        if _balance(test_user, mapping.department_id) is None:
            raise TestSenderError(f"Row {row.row_number}: the test faculty has no {mapping.code} sub-wallet; nothing was stored.")
        dept_ids[row.row_number] = mapping.department_id
    received_at = svc._message_date(msg)
    fy = financial_year(timezone.localtime(received_at))
    obj = SricWalletMailMessage.objects.filter(folder=folder, uid=uid).first()
    rerun = obj is not None
    if rerun and not obj.is_test:
        raise TestSenderError("The named email was stored by the normal scan, not as a test.")
    if obj is None:
        obj = SricWalletMailMessage.objects.create(
            folder=folder,
            uid=uid,
            message_id=" ".join(str(msg.get("Message-ID") or "").split())[:500],
            received_at=received_at,
            from_addr=parseaddr(msg.get("From") or "")[1].lower()[:255],
            trigger=TEST_TRIGGER,
            is_test=True,
            attachment_name=filename[:255],
            attachment_sha256=hashlib.sha256(content).hexdigest(),
            row_count=len(parsed.rows),
            authenticated=False,
            auth_verdict=f"test sender override (origin check bypassed); {auth.verdict}"[:500],
            status=SricWalletMailStatus.PROCESSED,
        )
    rows = []
    for row in parsed.rows:
        test_row = replace(row, ledger_id=test_ledger(row.ledger_id), employee_id=test_user.emp_id, pi_name=TEST_PI_NAME)
        dept_id = dept_ids[row.row_number]
        before = _balance(test_user, dept_id)
        rec = svc.store_row(test_row, message=obj, fy=fy, origin_verified=False, config=config, is_test=True)
        entry: dict[str, Any] = {"row_id": rec.pk, "row": rec.row_number, "stored_status": str(rec.status),
                                 "reason": rec.review_reason, "ledger_id": rec.ledger_id, "receiver": rec.receiver_code,
                                 "amount": f"{rec.amount:.2f}" if rec.amount is not None else None,
                                 "matched_test_faculty": rec.matched_user_id == test_user.pk,
                                 "duplicate_of": rec.duplicate_of_id, "balance_before": f"{before:.2f}"}
        try:
            rec, credited_now = svc.credit_row(rec.pk, actor=actor, note="SRIC CSV test (test sender run)")
            entry["credit"] = "credited" if credited_now else "already_credited"
        except svc.SricRechargeError as exc:
            entry["credit"] = f"refused:{exc.code}"
        rec.refresh_from_db()
        entry.update(
            final_status=str(rec.status),
            credit_key=rec.credit_key or "",
            wallet_transaction_id=rec.wallet_transaction_id,
            confirmation_email_sent=bool(rec.confirmation_sent_at),
            balance_after=f"{_balance(test_user, dept_id):.2f}",
        )
        entry.update(_visible(rec, test_user, actor))
        rows.append(entry)
    return {"message_record_id": obj.pk, "rerun": rerun, "rows": rows}


def run(
    *,
    test_sender: str,
    mode: str = "dry_run",
    folder: str = "",
    uid: str = "",
    test_user_id: int | None = None,
    actor_id: int | None = None,
) -> dict[str, Any]:
    """Find emails from ``test_sender`` and report them (dry run) or run the named one end to end (e2e)."""
    from django.conf import settings as dj_settings

    from iic_booking.users.imap_fetch import connect_imap
    from iic_booking.users.test_accounts import email_redirects

    config = SricWalletRechargeSettings.get_singleton()
    sender = _sender(test_sender, config)
    test_user = actor = None
    if mode == "e2e":
        if not (folder and uid):
            raise TestSenderError("Name the test email by folder and UID (from the dry run).")
        test_user = User.objects.filter(pk=test_user_id).first() if test_user_id else None
        ok, why = eligible_test_faculty(test_user)
        if not ok:
            raise TestSenderError(f"The test faculty account is not usable ({why}); nothing was done.")
        actor = _main_admin(actor_id)
    imap = svc._imap_config()
    if imap is None:
        raise TestSenderError("The portal mailbox is not configured.")
    primary = (getattr(dj_settings, "IMAP_MAILBOX", "") or "INBOX").strip() or "INBOX"
    cutoff = svc._cutoff_date()
    since = (cutoff - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    result: dict[str, Any] = {
        "mode": mode,
        "test_sender": mask(sender),
        "scanner_folder": primary,
        "cutoff": cutoff.isoformat(),
        "folders": [],
        "messages": [],
    }
    if mode == "e2e":
        result.update(test_user_id=test_user.pk, actor_id=actor.pk, test_mail_redirects=len(email_redirects()))
    conn = connect_imap(imap["host"], imap["port"], imap["use_ssl"], imap["email_address"], imap["password"], timeout=imap["timeout"])
    try:
        for name in _folder_names(conn, primary):
            typ, _ = conn.select(_quote(name), readonly=True)
            if typ != "OK":
                result["folders"].append({"folder": name, "selectable": False})
                continue
            safe = sender.replace("\\", "\\\\").replace('"', '\\"')
            typ, data = conn.uid("search", None, f'FROM "{safe}" SINCE {since}')
            uids = [u.decode() if isinstance(u, bytes) else str(u) for u in (data[0].split() if typ == "OK" and data and data[0] else [])]
            result["folders"].append({"folder": name, "selectable": True, "from_test_sender": len(uids)})
            for msg_uid in uids[-MAX_MESSAGES:]:
                raw = svc._fetch(conn, msg_uid)
                if raw is None:
                    continue
                msg = email.message_from_bytes(raw)
                received_at = svc._message_date(msg)
                files = svc._attachments(msg, config.attachment_name)
                auth = check_message(msg, config)
                entry: dict[str, Any] = {
                    "folder": name,
                    "uid": msg_uid,
                    "date": timezone.localtime(received_at).isoformat(timespec="minutes"),
                    "in_scanner_folder": name == primary,
                    "in_date_window": received_at.date() >= cutoff,
                    "attachment": bool(files),
                    "already_stored": SricWalletMailMessage.objects.filter(folder=name, uid=msg_uid).exists(),
                    "origin_check_real": auth.verdict,
                    "origin_verified_real": auth.authenticated,
                }
                if files and mode == "dry_run":
                    parsed = parse_wallet_recharge_csv(files[0][1])
                    fy = financial_year(timezone.localtime(received_at))
                    entry["parse_error"] = parsed.error
                    entry["rows"] = [plan_test_row(r, fy, config) for r in parsed.rows]
                result["messages"].append(entry)
                if mode == "e2e" and name == folder and msg_uid == uid:
                    if not files:
                        raise TestSenderError("The named email has no Wallet_Recharge.csv attachment.")
                    if not entry["in_date_window"]:
                        raise TestSenderError("The named email is older than the reading cutoff.")
                    result["e2e"] = _e2e(msg, files[0][1], files[0][0], folder=name, uid=msg_uid, auth=auth,
                                         config=config, test_user=test_user, actor=actor)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    if mode == "e2e" and "e2e" not in result:
        raise TestSenderError("The named test email was not found.")
    return result


def reverse(*, row_id: int, actor_id: int | None = None) -> dict[str, Any]:
    """Debit a credited test row back out of the test wallet (Main Admin ledger debit). Safe to repeat."""
    from iic_booking.users.admin_wallet_ledger import LedgerError, perform_adjustment

    rec = SricWalletRecharge.objects.select_related("sub_wallet", "matched_user").filter(pk=row_id).first()
    if rec is None or not rec.is_test:
        raise TestSenderError("Only a test row can be reversed here.")
    if rec.status != S.CREDITED or rec.sub_wallet_id is None:
        raise TestSenderError("The test row is not credited.")
    actor = _main_admin(actor_id)
    before = SubWallet.objects.get(pk=rec.sub_wallet_id).balance
    try:
        record, created = perform_adjustment(
            actor=actor,
            data={
                "client_request_id": f"sric-test-reversal-{rec.pk}",
                "owner_id": rec.matched_user_id,
                "sub_wallet_id": rec.sub_wallet_id,
                "direction": "debit",
                "amount": f"{rec.amount:.2f}",
                "reason": "correction",
                "remarks": REVERSAL_REMARKS,
                "external_reference": rec.reference,
                "notify_owner": False,
            },
        )
    except LedgerError as exc:
        raise TestSenderError(f"The reversal debit was refused ({exc.code}).") from exc
    if created:
        svc.set_verification(rec.pk, actor=actor, verified=False, remarks=f"Test credit reversed ({REVERSAL_REMARKS}, {record.reference}).")
    after = SubWallet.objects.get(pk=rec.sub_wallet_id).balance
    return {"row_id": rec.pk, "adjustment": record.reference, "created": created, "actor_id": actor.pk,
            "balance_before_reversal": f"{before:.2f}", "balance_after_reversal": f"{after:.2f}",
            "credited_amount": f"{rec.amount:.2f}", "balance_before_credit": f"{rec.balance_after - rec.amount:.2f}"}
