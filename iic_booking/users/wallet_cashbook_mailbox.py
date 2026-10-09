"""
Scheduled reader for SRIC cash-book emails.

Reads new emails from the configured SRIC senders (read-only IMAP, nothing is deleted), stores the
parsed cash-book rows and runs the usual conservative auto-match, which marks matching recharge
requests as fund-received (and approves pending ones).

The first run only records the messages already in the mailbox, so historical cash-books are
never auto-applied; admins can still import those manually from the Wallet Recharge Parse page.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache

from iic_booking.users.imap_fetch import fetch_email_attachment, list_emails
from iic_booking.users.models.wallet_sric_settings import WalletCashbookMailboxMessage, WalletSricSettings
from iic_booking.users.wallet_recharge_import import (
    match_pending_recharge_requests_to_parse_entries,
    split_cashbook_rows_by_cutoff,
    store_parsed_cashbook_rows,
)
from iic_booking.users.wallet_recharge_ops import _parse_sric_recipient_emails
from iic_booking.users.wallet_recharge_parser import parse_wallet_recharge_file

logger = logging.getLogger(__name__)

LOCK_KEY = "wallet_cashbook_mailbox_reader_lock"
LOCK_SECONDS = 25 * 60
DEFAULT_SENDER = "bills@sric.iitr.ac.in"
MAX_MESSAGES_PER_RUN = 25
BASELINE_UID = "baseline"


def _imap_config() -> dict[str, Any] | None:
    user = (getattr(settings, "IMAP_USER", "") or "").strip()
    password = getattr(settings, "IMAP_PASSWORD", "") or ""
    host = (getattr(settings, "IMAP_HOST", "") or "").strip()
    if not (user and password and host):
        return None
    return {
        "host": host,
        "port": int(getattr(settings, "IMAP_PORT", 993) or 993),
        "use_ssl": bool(getattr(settings, "IMAP_USE_SSL", True)),
        "email_address": user,
        "password": password,
        "timeout": float(getattr(settings, "IMAP_CONNECT_TIMEOUT", 20) or 20),
    }


def read_cashbook_mailbox(*, max_messages: int = MAX_MESSAGES_PER_RUN) -> dict[str, Any]:
    sric = WalletSricSettings.get_singleton()
    if not sric.auto_read_cashbook_mailbox:
        return {"status": "disabled"}
    config = _imap_config()
    if config is None:
        return {"status": "not_configured"}
    if not cache.add(LOCK_KEY, "1", LOCK_SECONDS):
        return {"status": "already_running"}
    try:
        return _read(sric, config, max_messages)
    finally:
        cache.delete(LOCK_KEY)


def _read(sric: WalletSricSettings, config: dict[str, Any], max_messages: int) -> dict[str, Any]:
    folder = (getattr(settings, "IMAP_MAILBOX", "") or "INBOX").strip() or "INBOX"
    senders = _parse_sric_recipient_emails(sric.cashbook_sender_emails or "") or [DEFAULT_SENDER]
    baseline = not WalletCashbookMailboxMessage.objects.filter(folder=folder, uid=BASELINE_UID).exists()
    seen = set(WalletCashbookMailboxMessage.objects.filter(folder=folder).values_list("uid", flat=True))

    processed = 0
    rows_stored = 0
    errors: list[str] = []
    for sender in senders:
        emails, error = list_emails(folder=folder, sender_filter=sender, max_results=50, **config)
        if error:
            errors.append(f"{sender}: {error}")
            continue
        for item in reversed(emails):
            uid = item.get("uid") or ""
            if not uid or uid in seen:
                continue
            record = {
                "subject": (item.get("subject") or "")[:500],
                "from_addr": (item.get("from_addr") or "")[:255],
            }
            if baseline:
                WalletCashbookMailboxMessage.objects.create(
                    folder=folder, uid=uid, error="Present before automatic reading started; not read.", **record
                )
                seen.add(uid)
                continue
            if processed >= max_messages:
                break
            content, filename, fetch_error = fetch_email_attachment(
                email_uid=uid, folder=folder, **config
            )
            rows = parse_wallet_recharge_file(content) if content else []
            eligible = split_cashbook_rows_by_cutoff(rows)[0] if rows else []
            stored = store_parsed_cashbook_rows(eligible, source_imap_uid=uid) if eligible else 0
            WalletCashbookMailboxMessage.objects.create(
                folder=folder,
                uid=uid,
                attachment_name=(filename or "")[:255],
                rows_parsed=len(rows),
                rows_stored=stored,
                error=fetch_error or ("" if rows else "No cash-book rows found."),
                **record,
            )
            seen.add(uid)
            processed += 1
            rows_stored += stored

    if baseline and not errors:
        WalletCashbookMailboxMessage.objects.get_or_create(
            folder=folder, uid=BASELINE_UID, defaults={"error": "Automatic reading started."}
        )

    matched, match_errors = (0, [])
    if rows_stored:
        matched, match_errors = match_pending_recharge_requests_to_parse_entries()
    result = {
        "status": "baseline" if baseline else "ok",
        "messages_read": processed,
        "rows_stored": rows_stored,
        "requests_matched": matched,
        "errors": errors + match_errors[:20],
    }
    if errors:
        logger.warning("SRIC cash-book mailbox reader errors: %s", errors)
    return result
