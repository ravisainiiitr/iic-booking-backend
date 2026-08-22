# Phase 10 — Production Phase-8 Deployment + Migration + Legacy Booking Qualification

**Date:** 2026-08-22  
**Mode:** Deploy/migrate/qualify — **T0 remains OFF**  
**Verdict:** **PRODUCTION MIGRATION BLOCKED — DO NOT PROCEED**

No T0 activation, blocks, emails, refunds, test cleanup, or booking modifications were performed.

---

## Executive summary

Phase 10 requires **two separated operator actions** (deploy, then migrate) followed by **read-only legacy qualification**. None of the production-side steps were executed in this session because:

1. **Phase 8A/8B/8C code exists on disk but is NOT committed to git** at the current branch tip (`81c012b`). Production remains at **`7d1081d`** / tag **`v2.5.2-channel-i-user-savepoint`**, which does **not** contain migrations `0101`–`0103` or bridge code.
2. **Deploy Backend** and **Migrate Production** (`confirm_migrate=MIGRATE`) workflows require explicit operator dispatch — not assumed here.
3. **MySQL booking column map** and **legacy discovery** require the production Django container with `OLD_MYSQL_*` RO credentials — tooling is ready (`legacy_booking_mysql.py`, `migration_production_legacy_qualification`).

Phase 10.1 **file preflight PASS** (20/20 artifacts on disk; `0188`/R14 absent). Test suites were **not run** (require staging `DATABASE_URL`).

---

## 10.1 — Pre-deploy local safety check

| Check | Result |
|-------|--------|
| Phase 8A refund (`migration_refund.py`, `0101`) | ✅ on disk |
| Phase 8B bridge (`booking_bridge.py`, `0102`) | ✅ on disk |
| Phase 8C notifications (`migration_notifications.py`, `0103`) | ✅ on disk |
| Production readiness commands | ✅ on disk |
| `equipment.0188` | ✅ **ABSENT** |
| R14 / `test_r14_*` | ✅ **ABSENT** |
| PR #86 | **NOT deployed** (per scope doc) |
| Phase 8A tests (22) | ⏸ NOT RUN |
| Phase 8B tests (21) | ⏸ NOT RUN |
| Phase 8C tests (11) | ⏸ NOT RUN |
| REAL integration (30) | ⏸ NOT RUN |
| **Git commit of Phase 8** | ❌ **NOT COMMITTED** |

```bash
# Local preflight (files only)
python manage.py migration_phase10_preflight
# With tests (staging DATABASE_URL required)
python manage.py migration_phase10_preflight --run-tests
```

**BLOCKER:** Commit + tag a reviewed Phase 8 release before production deploy.

---

## 10.2 — Production deployment

| Item | Value |
|------|-------|
| **Current production SHA** | `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea` |
| **Target SHA** | *Pending tagged release containing committed Phase 8* |
| **Target tag** | *To be assigned after commit + Backend Release qualification* |
| **Deployment result** | **NOT EXECUTED** |

Approved mechanism: **Deploy Backend** workflow (`backend-deploy.yml`) with immutable `release_tag`.

Post-deploy verify:
- `GET /api/version/` → currently **200 PASS** (2.5.2, build 2026-08-22)
- `GET /api/v1/analysis/health/ready/` → **200 PASS**
- Deployed SHA must equal approved target SHA

---

## 10.3 — Verify no auto-migrate

| Migration | Expected after deploy (before MIGRATE) |
|-----------|----------------------------------------|
| users.0101 | **[ ]** |
| users.0102 | **[ ]** |
| users.0103 | **[ ]** |

Production `/start` script: **no `manage.py migrate`** (verified in Phase 9 audit).

**Status:** Not re-verified post-deploy (deploy not run). Prior audit: auto-migrate **forbidden**.

---

## 10.4 — Backup before schema migration

| Item | Result |
|------|--------|
| Latest nightly | `nightly-20260821` |
| gzip | PASS (prior audit) |
| Pre-migrate fresh backup | **REQUIRED** immediately before `MIGRATE` |

---

## 10.5 — Migration plan

Run on production host **before** migrate:

```bash
docker exec -w /app <django> python manage.py migrate --plan
```

Expected pending: **users.0101, 0102, 0103 only**  
Forbidden in plan: **equipment.0188, R14**

**Status:** NOT EXECUTED (migrate not approved)

---

## 10.6 — Explicit migration approval

Workflow: **Migrate Production** (`migrate-production.yml`)  
Required input: `confirm_migrate=MIGRATE`

**Operator approval:** **NOT PROVIDED** in this phase  
**Migrate executed:** **NO**

---

## 10.7 — Post-migration verification

| Check | Status |
|-------|--------|
| users.0101 [X] | ❌ NOT APPLIED |
| users.0102 [X] | ❌ NOT APPLIED |
| users.0103 [X] | ❌ NOT APPLIED |
| 0096–0100 [X] | ✅ APPLIED |
| equipment.0188 | ✅ NOT APPLIED |
| Schema tables | ❌ Absent until 0102/0103 |

Required tables after migrate:
- `MigrationBookingSettlement` (0101)
- `LegacyEquipmentMapping`, `LegacyBookingBlock`, `LegacyBookingMigrationBatch` (0102)
- `MigrationNotificationBatch` (0103)

---

## 10.8 — Equipment mapping

**Status:** **BLOCKED** (0102 not applied)

Rules enforced in code:
- Explicit mapping only — **no fuzzy matching**
- `validate_legacy_equipment_mapping` command

Required before T0:
- unmapped eligible equipment = **0**
- duplicate mappings = **0**

---

## 10.9 — Legacy MySQL booking column map

**Status:** **NOT EXECUTED on production**

Tooling: `iic_booking/users/legacy_ledger/legacy_booking_mysql.py`

Method:
1. `SHOW COLUMNS FROM booking` (read-only)
2. Resolve semantic fields only when **exactly one** candidate column matches
3. If ambiguous → **BLOCK** until operator supplies `--column-map-file`

Connection (production):
```
OLD_MYSQL_HOST=host.docker.internal
OLD_MYSQL_PORT=3306
OLD_MYSQL_DATABASE=admin
OLD_MYSQL_USER=iic_booking_ro
```

Evidence doc: column names + types only — **no passwords, no PII, no sample rows**.

---

## 10.10–10.13 — Legacy discovery, identity, slots, conflicts

**Status:** NOT EXECUTED (blocked by 10.7 + 10.9)

Read-only pipeline (no blocks, no Booking copies):

```bash
python manage.py migration_production_legacy_qualification \
  [--column-map-file /secure/booking_column_map.json] \
  --json-out /tmp/phase10.json
```

Classifications: ELIGIBLE, CANCELLED, COMPLETED, OUTSIDE_WINDOW, UNMAPPED_EQUIPMENT, CONFLICTING, DUPLICATE, identity EXCEPTION.

Identity: legacy `users.emp_id` → new portal `User.emp_id` (exactly one match required).

Slot audit: overlapping `DailySlot` read-only; flags `EXISTING_NEW_BOOKING` conflicts.

---

## 10.14 — Test account dry-run

**NOT EXECUTED on production**

```bash
python manage.py migration_cleanup_test_accounts --dry-run
```

Criterion: `User.is_test_account == True` only.

---

## 10.15 — Email recipient dry-run

**NOT EXECUTED on production** | **Emails sent: 0**

Classification policy (code):
| Role | Template / policy |
|------|-------------------|
| Faculty | `FACULTY_MIGRATION` |
| Student | `STUDENT_MIGRATION` |
| OIC | `OIC_MIGRATION` |
| Main Admin | `ADMIN_MIGRATION` |
| Lab-in-Charge | Manual briefing — **no auto template** |
| Department Admin | Manual briefing |
| Normal / external | Excluded from blast |

Uses `select_notification_candidates()` only — **never** queues SMTP in qualification.

---

## 10.16–10.23 — Code verification (not activated)

| Area | Result |
|------|--------|
| Email templates (4) | ✅ code verified |
| Old portal freeze contract | ✅ `MIGRATION_BOOKING_DISABLED` |
| New portal 409 blocking | ✅ `LEGACY_MIGRATION_SLOT_BLOCKED` (staging tests) |
| Main Admin global view | ✅ server-side RBAC |
| OIC refund scope | ✅ equipment-scoped |
| Main Admin refund | ✅ global |
| Abort / reconciliation | ✅ `migration_abort_batch`, batch audit |

**T0 NOT activated:** `booking_migration_mode` remains NORMAL on production.

---

## 10.21 — T0 dataset summary

| Metric | Value |
|--------|------:|
| Eligible legacy bookings | — |
| Cancelled | — |
| Completed | — |
| Outside window | — |
| Unmapped equipment | — |
| Unresolved identities | — |
| Target slots | — |
| Slot conflicts | — |
| Existing new-booking conflicts | — |
| Duplicate eligible records | — |
| Test accounts | — |
| Email recipients | — |

---

## 10.24 — Operator workflow (remaining)

1. **Commit + tag** Phase 8 release (no PR #86 / 0188 / R14)
2. Run **Phase 8 + REAL tests** on staging DB
3. **Deploy Backend** with approved `release_tag`
4. Verify SHA, health, **0101–0103 still [ ]**
5. **Fresh backup** + gzip verify
6. `migrate --plan` — confirm only 0101–0103
7. **Migrate Production** with `MIGRATE`
8. Post-migrate schema verify
9. **Load equipment mappings**
10. Set `migration_start_at` / `migration_window_end_at`
11. Run **Phase 10 Production Legacy Qualification** workflow
12. Review JSON + this doc → **Phase 11 GO** (separate authorization)

Workflow: `.github/workflows/phase10-production-qualification.yml`

---

## 10.25 — HARD STOP

**T0 was NOT activated.** No blocks, slot changes, emails, refunds, or cleanup.

Even if all gates pass after operator steps → return:

**PRODUCTION T0 READY — AWAITING EXPLICIT OPERATOR APPROVAL**

Only Phase 11 may perform T0.

---

## Final report (28 items)

| # | Item | Result |
|---|------|--------|
| 1 | Target production SHA | **NOT TAGGED** (Phase 8 uncommitted) |
| 2 | Actual deployed SHA | `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea` |
| 3 | Production tag | `v2.5.2-channel-i-user-savepoint` |
| 4 | Deployment result | **NOT EXECUTED** |
| 5 | Migration 0101 | **NOT APPLIED** |
| 6 | Migration 0102 | **NOT APPLIED** |
| 7 | Migration 0103 | **NOT APPLIED** |
| 8 | Unexpected migration | **NONE** (0188/R14 absent in repo) |
| 9 | Backup | **PASS** (prior); fresh backup required before migrate |
| 10 | MySQL column-map | **NOT EXECUTED** |
| 11 | Equipment mapping | **BLOCKED** (0102) |
| 12 | Upcoming-week legacy count | **NOT EXECUTED** |
| 13 | Eligible count | **NOT EXECUTED** |
| 14 | Unmapped booking count | **NOT EXECUTED** |
| 15 | Identity exception count | **NOT EXECUTED** |
| 16 | Target slot count | **NOT EXECUTED** |
| 17 | Slot conflict count | **NOT EXECUTED** |
| 18 | Existing new-booking conflicts | **NOT EXECUTED** |
| 19 | Test-account dry-run | **NOT EXECUTED** |
| 20 | Email recipient dry-run | **NOT EXECUTED** |
| 21 | Emails actually sent | **0** |
| 22 | Freeze verification | **PASS (code; not activated)** |
| 23 | New portal blocking | **PASS (code; not activated)** |
| 24 | Refund RBAC | **PASS (code)** |
| 25 | Main Admin global view | **PASS (code)** |
| 26 | Reconciliation readiness | **PASS (code)** |
| 27 | Abort readiness | **PASS (code)** |
| 28 | Production writes this phase | **ZERO** |

### Verdict

# PRODUCTION MIGRATION BLOCKED — DO NOT PROCEED

Primary blockers: **uncommitted Phase 8 code**, **no production deploy**, **no gated migrate**, **no MySQL legacy qualification on production**.
