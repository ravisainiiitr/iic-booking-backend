# Phase 8C — Staging Migration Simulation

**Environment:** STAGING / isolated test DB only  
**Production T0 / migrate / emails / refunds / blocks:** **NOT PERFORMED**

## Pre-flight

| Suite | Result |
|-------|--------|
| Phase 8A (`test_migration_refund_settlement`) | **22 PASS** |
| Phase 8B (`test_phase8b_legacy_booking_bridge`) | **21 PASS** |
| Combined re-run | **43 PASS** |
| Phase 8C (`test_phase8c_staging_simulation`) | **11 PASS** |

Migrations available: `users.0096`–`0103` (0101 Phase 8A, 0102 Phase 8B, 0103 Phase 8C notifications).  
`equipment.0188` / R14: **not** applied.

Isolated DB used for this phase: staging Postgres `iic_booking_test_8b` (not production RDS).

## Final verdict

**PHASE 8C STAGING SIMULATION PASS — READY FOR PRODUCTION REVIEW**

Production migration still requires a separate GO/NO-GO. Staging PASS does not authorize production T0.

## Staging sequence executed

1. Validate equipment mappings (explicit only)
2. Configure `migration_start_at` / `migration_window_end_at` / `new_portal_url` (app TZ)
3. Test-account cleanup dry-run (`is_test_account` only)
4. Fixture legacy bookings (10 controlled scenarios — **no production copy**)
5. Discovery classification
6. `migration_dry_run` (no writes)
7. Staging T0 (`run_staging_t0` / `migration_staging_t0`) after READY
8. Old-portal freeze signal `MIGRATION_BOOKING_DISABLED`
9. New-portal `LEGACY_MIGRATION_SLOT_BLOCKED` (409)
10. Notification batch dry-run / optional Mailpit queue
11. Reconciliation
12. Abort batch (blocks released; audit retained; finances not reversed)

## Role matrix (freeze)

| Role | View | New booking (old) | Migration refund |
|------|------|-------------------|------------------|
| Faculty/Student | YES | 403 | NO |
| Lab-in-Charge | YES + operational | 403 | NO |
| OIC | YES + operational | 403 | YES |
| Dept Admin | YES | 403 | NO |
| Main Admin | Global YES | 403 | YES |

## Production safety

- `DEPLOYMENT_ENVIRONMENT=PRODUCTION` → T0 / live notification queue **refused**
- Phase 8C tools target staging Mailpit / locmem
- Separate production GO/NO-GO still required
