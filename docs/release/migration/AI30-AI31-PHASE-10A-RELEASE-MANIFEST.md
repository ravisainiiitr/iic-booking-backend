# Phase 10A — Release Manifest

**Release branch:** `release/portal-migration-phase8-production-candidate`  
**Base production SHA:** `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea`  
**Tag (post-commit):** `v2.5.2-portal-migration-production-candidate`  
**Date:** 2026-08-22  

---

## Included — Phase 8A

| Category | Paths |
|----------|-------|
| Migration | `users/migrations/0101_migration_booking_settlement.py` |
| Service | `users/legacy_ledger/migration_refund.py` |
| APIs | `users/api/portal_migration_views.py` (settlement + refund endpoints) |
| Models | `MigrationBookingSettlement` in `users/models/portal_migration.py` |
| Tests | `users/tests/test_migration_refund_settlement.py` (22 tests) |
| Docs | `AI30-AI31-PHASE-8A-MIGRATION-REFUND.md` |

## Included — Phase 8B

| Category | Paths |
|----------|-------|
| Migration | `users/migrations/0102_legacy_equipment_booking_bridge.py` |
| Services | `booking_bridge.py`, `equipment_mapping.py`, `migration_dry_run.py` |
| APIs | `users/api/portal_legacy_bridge_views.py` |
| Slot hook | `equipment/api_views.py` → `LEGACY_MIGRATION_SLOT_BLOCKED` |
| Commands | `validate_legacy_equipment_mapping`, `migration_discover_legacy_bookings`, `migration_dry_run`, `migration_reconcile_legacy_blocks`, `migration_abort_batch`, `migration_cleanup_test_accounts`, `migration_staging_t0` |
| Tests | `users/tests/test_phase8b_legacy_booking_bridge.py` (21 tests) |
| Docs | Phase 8B architecture/mapping/blocking/runbook |

## Included — Phase 8C

| Category | Paths |
|----------|-------|
| Migration | `users/migrations/0103_migration_notification_batch.py` |
| Services | `migration_emails.py`, `migration_notifications.py`, `migration_t0.py` |
| Celery | `users/tasks.py` → `send_migration_notification_recipient` |
| Commands | `migration_notification_dry_run`, `migration_email_preview` |
| Tests | `users/tests/test_phase8c_staging_simulation.py` (11 tests) |
| Docs | Phase 8C emails/simulation/T0/reconciliation |

## Included — Phase 9/10 qualification

| Category | Paths |
|----------|-------|
| Commands | `migration_production_t0_readiness`, `migration_production_legacy_qualification`, `migration_phase10_preflight` |
| MySQL RO | `users/legacy_ledger/legacy_booking_mysql.py` |
| Workflow | `.github/workflows/phase10-production-qualification.yml` |
| Test runner | `scripts/staging/run_phase8_test_suite.sh` |
| Docs | Phase 9/10 readiness + production audit evidence JSON |

## Included — runtime wiring

- `config/api_router.py` — portal legacy bridge + migration refund routes
- `users/models/__init__.py` — model exports
- `users/legacy_ledger/booking_lock.py` — old portal freeze signal
- `users/models/portal_migration.py` — extended state + all Phase 8 models

---

## Excluded

| Item | Reason |
|------|--------|
| `equipment.0188` / `0189` | Forbidden R14 lineage — **absent** from this branch |
| `docs/release/phase-R14/**` | Forbidden |
| `test_r14_*` | Forbidden |
| PR #86 branch | **Untouched** — separate open PR |
| `.envs/.production/.django` | Secret — not tracked |
| `.envs/.staging/.django` | Secret — not tracked |
| Probe-only CI commits on `probe/production-real-ro-ro-fdda7be` | Not part of this release ancestry |
| Frontend UI (`iic-booking-frontend`) | **Separate repo** — companion changes exist locally uncommitted |

---

## Migrations (users)

```
0096 → 0097 → 0098 → 0099 → 0100 → 0101 → 0102 → 0103
```

- `0101` depends on `0100` + `equipment.0187`
- `0102` depends on `0101`
- `0103` depends on `0102`
- **No `equipment.0188`** on this branch (max equipment migration: `0187`)

Staging test DB `test_iic_booking_test_8b`: all `0101`–`0103` applied; no pending plan entries.

---

## Test qualification (staging PostgreSQL)

Executed via `scripts/staging/run_phase8_test_suite.sh` on staging Docker (`test_iic_booking_test_8b`):

| Suite | Expected | Actual |
|-------|----------|--------|
| Phase 8A (`test_migration_refund_settlement`) | 22 | **22 PASS** |
| Phase 8B (`test_phase8b_legacy_booking_bridge`) | 21 | **21 PASS** |
| Phase 8C (`test_phase8c_staging_simulation`) | 11 | **11 PASS** |
| REAL integration (preflight + activation) | 30 | **30 PASS** |
| Production hard-OFF + no-auto-migrate | 8 | **8 PASS** |
| **Total** | **92** | **92 PASS** |

---

## Production safety

| Check | Result |
|-------|--------|
| `REAL_INTEGRATION_ENABLED=False` in `production.py` | PASS |
| Fixture modes hard-OFF | PASS |
| `LOCAL_STAGING_ACCEPTED=False` | PASS |
| `compose/production/django/start` — no executable migrate | PASS |
| `backend-deploy.yml` — no auto-migrate | PASS |
| `migrate-production.yml` — requires `MIGRATE` | PASS |

---

## R14 / 0188 exclusion audit

- Filesystem search: **no** `0188`, **no** `phase-R14`, **no** `test_r14_*`
- Git diff (staged): **no** equipment migrations beyond `0187`

---

## Secret audit

- No `.env` production/staging files staged
- Test passwords are synthetic (`test-pass-not-used`) only
- No API keys, OAuth secrets, or private keys in staged diff

---

## Not performed (by design)

- Production deployment
- Production migration (`MIGRATE`)
- T0 activation
- Production emails / refunds / blocks
- PR merge or auto-merge

---

## Companion note

Frontend settlement UI, `AdminPortalMigration`, and `MigrationPortalBanner` changes exist **uncommitted** in `iic-booking-frontend` and require a **separate frontend release PR** before production deploy of the full migration UX.
