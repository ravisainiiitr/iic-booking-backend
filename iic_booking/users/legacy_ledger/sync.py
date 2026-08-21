"""Run mapping, dry-run, and ledger import without loading the whole old DB."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from time import perf_counter

from django.conf import settings
from django.db.utils import ProgrammingError

from iic_booking.users.legacy_ledger.importer import (
    import_transaction,
    record_processed_watermark,
    record_sync_failure,
)
from iic_booking.users.legacy_ledger.mapping import (
    classify_old_user,
    load_new_users_by_employee_id,
    mapping_report_dict,
    upsert_mapping,
)
from iic_booking.users.legacy_ledger.reader import OldMySQLReader
from iic_booking.users.models.portal_migration import (
    PortalMigrationState,
)


VALID_STATUSES = {"VALID", "MAPPED", "IMPORTED", "RECONCILED"}


def run_mapping(
    reader: OldMySQLReader,
    batch: str,
    dry_run: bool = True,
    *,
    require_verified_identity: bool = True,
    identity_source: str = "LEGACY_UNVERIFIED",
) -> dict:
    duplicates = reader.duplicate_employee_ids()
    new_users_by_emp = load_new_users_by_employee_id()
    wallets = reader.wallets_by_user_id() if hasattr(reader, "wallets_by_user_id") else {}
    totals_map = reader.ledger_totals_by_user_id() if hasattr(reader, "ledger_totals_by_user_id") else {}
    counts = Counter()
    rows = []
    credits_all = Decimal("0")
    debits_all = Decimal("0")
    for user in reader.iter_users(batch_size=int(getattr(settings, "PORTAL_MIGRATION_BATCH_SIZE", 500))):
        classified = classify_old_user(
            user,
            duplicates,
            new_users_by_emp,
            require_verified_identity=require_verified_identity,
            identity_source=identity_source,
        )
        counts[classified.mapping_status] += 1
        uid = int(user["id"])
        wallet = wallets.get(uid)
        if wallet is None and hasattr(reader, "wallet_for_user"):
            wallet = reader.wallet_for_user(uid)
        if uid in totals_map:
            credits, debits = totals_map[uid]
        else:
            credits, debits = reader.user_ledger_totals(uid)
        credits_all += credits
        debits_all += debits
        balance = Decimal(str(wallet["balance"])) if wallet and wallet.get("balance") is not None else None
        rec = mapping_report_dict(classified)
        rec["new_name"] = classified.channel_i_name
        rec["new_email"] = classified.channel_i_email
        rec["old_credits"] = str(credits)
        rec["old_debits"] = str(debits)
        rec["old_wallet_balance"] = str(balance) if balance is not None else None
        rec["calculated_closing_balance"] = str(credits - debits)
        rec["ledger_minus_wallet"] = (
            str((credits - debits) - balance) if balance is not None else None
        )
        rows.append(rec)
        if not dry_run:
            upsert_mapping(
                classified,
                wallet_id=wallet["id"] if wallet else None,
                old_credits=credits,
                old_debits=debits,
                old_balance=balance,
                batch=batch,
            )
    exceptions = [r for r in rows if r["mapping_status"] not in VALID_STATUSES]
    return {
        "counts": dict(counts),
        "rows": rows,
        "dry_run": dry_run,
        "batch": batch,
        "old_wallet_accounts": len(rows),
        "valid_employee_ids": counts.get("VALID", 0),
        "exception_count": len(exceptions),
        "credit_total": str(credits_all),
        "debit_total": str(debits_all),
        "calculated_balance": str(credits_all - debits_all),
        "exceptions": exceptions,
    }


def run_ledger_sync(reader: OldMySQLReader, batch: str, dry_run: bool = False, limit: int | None = None) -> dict:
    after_id = 0
    frozen = False
    if dry_run:
        existing = None
        try:
            existing = PortalMigrationState.objects.filter(singleton_key="default").first()
        except ProgrammingError:
            existing = None
        if existing:
            after_id = existing.last_wallet_txn_watermark
            frozen = existing.legacy_ledger_frozen
    else:
        state = PortalMigrationState.get_solo()
        frozen = state.legacy_ledger_frozen
        after_id = state.last_wallet_txn_watermark
    if frozen and not dry_run:
        return {"ok": False, "error": "Legacy ledger is frozen; incremental sync is stopped.", "imported": 0}
    if dry_run:
        totals = reader.transaction_stream_stats(after_id)
        processed = int(totals.get("n") or 0)
        if limit is not None:
            processed = min(processed, limit)
        return {
            "ok": True,
            "dry_run": True,
            "from_watermark": after_id,
            "to_watermark": after_id,
            "processed": processed,
            "stats": {"would_process": processed},
            "batch": batch,
            "min_id": totals.get("min_id"),
            "max_id": totals.get("max_id"),
            "wrote_financial_records": False,
        }
    batch_size = int(getattr(settings, "PORTAL_MIGRATION_BATCH_SIZE", 500))
    stats = Counter()
    processed = 0
    last_confirmed = after_id
    pending_users: dict[int, dict] = {}
    started = perf_counter()
    try:
        for txn in reader.iter_wallet_transactions(after_id, batch_size):
            uid = int(txn["user_id"])
            if uid not in pending_users:
                pending_users.update(reader.users_by_ids([uid]))
            if dry_run:
                stats["would_process"] += 1
            else:
                result = import_transaction(txn, pending_users.get(uid), batch)
                stats[result] += 1
                record_processed_watermark(int(txn["id"]), imported=(result == "imported"), batch=batch)
                last_confirmed = int(txn["id"])
            processed += 1
            if limit is not None and processed >= limit:
                break
        duration_ms = int((perf_counter() - started) * 1000)
        if not dry_run:
            state = PortalMigrationState.get_solo()
            state.sync_runs_total = (state.sync_runs_total or 0) + 1
            state.last_sync_duration_ms = duration_ms
            state.last_sync_imported_count = stats.get("imported", 0)
            state.last_sync_processed_count = processed
            state.last_sync_batch = batch
            state.save(
                update_fields=[
                    "sync_runs_total",
                    "last_sync_duration_ms",
                    "last_sync_imported_count",
                    "last_sync_processed_count",
                    "last_sync_batch",
                    "updated_at",
                ]
            )
        return {
            "ok": True,
            "dry_run": dry_run,
            "from_watermark": after_id,
            "to_watermark": last_confirmed if not dry_run else after_id,
            "processed": processed,
            "stats": dict(stats),
            "batch": batch,
            "duration_ms": duration_ms,
        }
    except Exception as exc:  # noqa: BLE001
        if not dry_run:
            record_sync_failure(type(exc).__name__)
        return {
            "ok": False,
            "error": type(exc).__name__,
            "processed": processed,
            "stats": dict(stats),
            "from_watermark": after_id,
            "to_watermark": last_confirmed,
            "resumes_from": last_confirmed,
        }


def dry_run_report(mapping: dict, ledger: dict) -> dict:
    return {
        "old_wallet_accounts": mapping.get("old_wallet_accounts"),
        "valid_employee_ids": mapping.get("valid_employee_ids"),
        "invalid_employee_ids": mapping.get("exception_count"),
        "transaction_count_would_process": (ledger.get("stats") or {}).get("would_process"),
        "credit_total": mapping.get("credit_total"),
        "debit_total": mapping.get("debit_total"),
        "calculated_balance": mapping.get("calculated_balance"),
        "expected_imported_balance": mapping.get("calculated_balance"),
        "duplicate_transactions": (ledger.get("stats") or {}).get("duplicate", 0),
        "exceptions": mapping.get("exception_count"),
        "reconciliation_status": "DRY_RUN_NOT_APPLIED",
        "watermark_unchanged": True,
        "from_watermark": ledger.get("from_watermark"),
        "ok": bool(ledger.get("ok")),
    }
