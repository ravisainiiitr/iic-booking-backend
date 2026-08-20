"""Import old wallet_transactions into the immutable legacy ledger."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.db import IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.users.legacy_ledger.mapping import exact_employee_id
from iic_booking.users.models.portal_migration import (
    LegacyLedgerDirection,
    LegacyWalletAccountMapping,
    LegacyWalletLedgerEntry,
    LegacyWalletMappingStatus,
    LegacyWalletSyncDeadLetter,
    PortalMigrationState,
)

UTR_RE = re.compile(r"\b(?:UTR|UPI|NEFT|IMPS)[:\s-]*([A-Za-z0-9]+)", re.I)
REF_RE = re.compile(r"\b(?:Ref|Reference|Receipt)[:\s-]*([A-Za-z0-9._/-]+)", re.I)


def extract_utr_and_reference(description: str) -> tuple[str, str]:
    text = description or ""
    utr = ""
    m = UTR_RE.search(text)
    if m:
        utr = m.group(1)[:64]
    ref = ""
    m2 = REF_RE.search(text)
    if m2:
        ref = m2.group(1)[:255]
    return utr, ref


def ledger_checksum(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _occurred_at(raw) -> datetime:
    if raw is None:
        return timezone.now()
    if isinstance(raw, datetime):
        dt = raw
    else:
        dt = parse_datetime(str(raw)) or timezone.now()
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _direction(transaction_type) -> str | None:
    try:
        code = int(transaction_type)
    except (TypeError, ValueError):
        return None
    if code == 1:
        return LegacyLedgerDirection.CREDIT
    if code == 2:
        return LegacyLedgerDirection.DEBIT
    return None


def import_transaction(txn: dict, user_row: dict | None, batch: str) -> str:
    """
    Returns: imported | duplicate | dead_letter | skipped_unmapped
    """
    source_id = int(txn["id"])
    if LegacyWalletLedgerEntry.objects.filter(
        source_system=LegacyWalletLedgerEntry.SOURCE_OLD_PORTAL,
        source_transaction_id=source_id,
    ).exists():
        return "duplicate"

    source_user_id = int(txn["user_id"])
    emp = exact_employee_id((user_row or {}).get("emp_id"))
    mapping = None
    if emp:
        mapping = LegacyWalletAccountMapping.objects.filter(employee_id=emp).first()
    if mapping is None or mapping.mapping_status not in (
        LegacyWalletMappingStatus.VALID,
        LegacyWalletMappingStatus.MAPPED,
        LegacyWalletMappingStatus.IMPORTED,
        LegacyWalletMappingStatus.RECONCILED,
    ):
        LegacyWalletSyncDeadLetter.objects.update_or_create(
            source_transaction_id=source_id,
            defaults={
                "source_user_id": source_user_id,
                "employee_id": emp,
                "reason": "UNMAPPED",
                "detail": "No VALID Employee-ID mapping; not imported automatically.",
                "payload": {
                    "amount": str(txn.get("amount")),
                    "transaction_type": txn.get("transaction_type"),
                    "create_date": str(txn.get("create_date")),
                },
            },
        )
        return "dead_letter"

    direction = _direction(txn.get("transaction_type"))
    if not direction:
        LegacyWalletSyncDeadLetter.objects.update_or_create(
            source_transaction_id=source_id,
            defaults={
                "source_user_id": source_user_id,
                "employee_id": emp,
                "reason": "UNKNOWN_TYPE",
                "detail": f"transaction_type={txn.get('transaction_type')}",
                "payload": {"raw_type": txn.get("transaction_type")},
            },
        )
        return "dead_letter"

    amount = abs(Decimal(str(txn.get("amount") or "0"))).quantize(Decimal("0.01"))
    description = str(txn.get("description") or "")
    utr, reference = extract_utr_and_reference(description)
    running = txn.get("balance")
    checksum_payload = {
        "id": source_id,
        "user_id": source_user_id,
        "amount": str(amount),
        "balance": str(running) if running is not None else "",
        "transaction_type": txn.get("transaction_type"),
        "create_date": str(txn.get("create_date")),
        "description": description,
    }
    try:
        LegacyWalletLedgerEntry.objects.create(
            mapping=mapping,
            employee_id=emp,
            source_system=LegacyWalletLedgerEntry.SOURCE_OLD_PORTAL,
            source_transaction_id=source_id,
            source_wallet_id=mapping.old_wallet_id,
            source_user_id=source_user_id,
            occurred_at=_occurred_at(txn.get("create_date")),
            direction=direction,
            amount=amount,
            running_balance_source=Decimal(str(running)) if running is not None else None,
            description=description,
            reference=reference,
            utr=utr,
            migration_batch=batch,
            checksum=ledger_checksum(checksum_payload),
        )
    except IntegrityError:
        return "duplicate"
    except ValueError:
        return "duplicate"

    if direction == LegacyLedgerDirection.CREDIT:
        mapping.imported_credits = (mapping.imported_credits or 0) + amount
    else:
        mapping.imported_debits = (mapping.imported_debits or 0) + amount
    mapping.mapping_status = LegacyWalletMappingStatus.IMPORTED
    mapping.save(
        update_fields=["imported_credits", "imported_debits", "mapping_status", "updated_at"]
    )
    return "imported"


def record_processed_watermark(source_id: int, *, imported: bool, batch: str):
    """Advance watermark only after this source id was handled (import, duplicate, or dead-letter)."""
    state = PortalMigrationState.get_solo()
    fields = ["last_sync_at", "last_sync_error", "last_sync_batch", "updated_at"]
    if source_id > state.last_wallet_txn_watermark:
        state.last_wallet_txn_watermark = source_id
        fields.append("last_wallet_txn_watermark")
    state.last_sync_at = timezone.now()
    state.last_sync_error = ""
    state.last_sync_batch = batch
    if imported:
        state.transactions_imported_total = (state.transactions_imported_total or 0) + 1
        fields.append("transactions_imported_total")
    state.save(update_fields=fields)


def record_sync_failure(error: str):
    state = PortalMigrationState.get_solo()
    state.last_sync_at = timezone.now()
    state.last_sync_error = (error or "")[:4000]
    state.sync_failures_total = (state.sync_failures_total or 0) + 1
    state.save(update_fields=["last_sync_at", "last_sync_error", "sync_failures_total", "updated_at"])


def advance_watermark(max_seen_id: int, error: str = ""):
    """Compatibility wrapper. Failed runs must pass error and must not raise watermark."""
    if error:
        record_sync_failure(error)
        return
    record_processed_watermark(max_seen_id, imported=False, batch="")
