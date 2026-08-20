# Portal Migration — Wallet Sync & Continuity

## Architecture

Legacy MySQL (read-only) → incremental sync by transaction id watermark → `LegacyWalletLedgerEntry` (immutable, UNIQUE source_system+source_transaction_id) → user legacy balance/statement APIs → at cutover: freeze sync, create opening balance once, new portal ledger authoritative.

## Transition UX

- Label: "Balance from previous IIC Booking Portal"
- Show last synchronized timestamp; on sync failure show last known + warning (no fabricated balance)

## Wallet identity

Exact verified Employee ID only. Exceptions: MISSING_EMPLOYEE_ID, DUPLICATE_EMPLOYEE_ID, CHANNEL_I_NOT_FOUND, etc.

## Opening balance

`legacy_wallet_import.MIGRATION_DESC_PREFIX` / one-time migration credit; never re-import after `legacy_ledger_frozen`.

Celery: `users.sync_legacy_wallet_ledger` (when incremental_sync_enabled and not frozen).
