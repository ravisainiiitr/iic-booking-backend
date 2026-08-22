# Phase 8A — Migration Refund / Settlement Authority

## Summary

During old-portal freeze / migration mode, **Officer-in-Charge (OIC)** and **Main Administrator** may issue a **one-time `MIGRATION_REFUND`** for eligible portal bookings.

This is **financial settlement only**. It does **not**:

- unlock end-user booking freeze
- free slots for a new booking
- create a new booking
- grant refund authority to Normal/Faculty/Lab-in-Charge/Department Admin

## Reuse of existing finance

Refund credits go through **`SubWallet.credit()`** (existing ledger).  
Balances are never updated by direct SQL / balance shortcuts.

## Model

`MigrationBookingSettlement` (`users` migration **0101**):

- Unique **COMPLETED** `MIGRATION_REFUND` per booking (DB constraint)
- Statuses: `PENDING` | `COMPLETED` | `FAILED` | `REJECTED`
- Audit: processed_by, role, amounts, reason, reference `MIG-REF-{booking_id}-{settlement_id}`, wallet_transaction FK

## APIs

| Method | Path | Who |
|--------|------|-----|
| GET | `/api/bookings/{id}/migration-settlement/` | OIC / Main Admin (scoped) |
| POST | `/api/bookings/{id}/migration-refund/` | OIC / Main Admin; body `{ "confirm": true, "reason": "..." }` |
| GET | `/api/portal-migration/admin/settlements/` | Report + filters |

Duplicate completed refund → **409** `"Migration refund already processed."`

## Window

Open when `end_user_booking_enabled=False`, or `legacy_ledger_frozen`, or phase in  
`FINANCIAL_FREEZE` / `FINAL_SYNC` / `RECONCILIATION` / `OLD_PORTAL_READ_ONLY`.

## Tests

`iic_booking/users/tests/test_migration_refund_settlement.py` — 22 cases covering RBAC, idempotency, ledger, freeze safety, scope.

## Frontend

`BookingDetailCard` — Migration Settlement panel + confirm dialog (OIC / Main Admin only).

## Safety

Do **not** run against production financial data during development.  
Apply migration **0101** only via the gated production migrate workflow when approved.
