# Phase 10B — Production Deploy + Read-Only Qualification (COMPLETE)

**Date:** 2026-08-22  
**Verdict:** **NOT READY — BLOCKERS LISTED**  
*(Deploy PASS; migration/T0 still blocked)*

**T0:** NOT ACTIVATED | **migrate:** NOT RUN | **Production data writes:** deploy only

---

## PR #90 — Verified merged

| Field | Value |
|-------|-------|
| State | **MERGED** |
| mergedAt | `2026-08-22T07:14:28Z` |
| **Merge commit SHA** | **`6cf24bf24fa2809c6e4287e2baca3b6e24dd5f1b`** |
| Pre-merge candidate | `9666952` (branch tip — **not** used as deploy SHA) |
| PR #86 | **OPEN — UNTOUCHED** ✓ |

---

## Deploy

| Field | Value |
|-------|-------|
| Release tag | `v2.5.2-portal-migration-phase8-production` → `6cf24bf` |
| Deploy workflow | [#32559599412](https://github.com/ravisainiiitr/iic-booking-backend/actions/runs/32559599412) |
| EC2 HEAD | `6cf24bf` ✓ **SHA MATCH** |
| Previous tag | `v2.5.2-channel-i-user-savepoint` |
| Auto-migrate | **NONE** ✓ |

---

## Health

| Check | Result |
|-------|--------|
| `/api/version/` | PASS (2.5.2) |
| Readiness | PASS (DB ok, cache ok) |
| Django | PASS |
| Celery worker/beat | PASS |
| Redis | PASS (healthy) |
| Frontend gate | PASS |

---

## Migrations (read-only)

| Migration | State |
|-----------|-------|
| users.0096–0100 | **[X] APPLIED** |
| users.0101–0103 | **[ ] PENDING** |
| equipment.0188 / R14 | **ABSENT** |

`migrate --plan`: only 0101, 0102, 0103 — **no forbidden migrations**.

---

## Backup

| Field | Value |
|-------|-------|
| Identifier | `nightly-20260822` |
| File | `portal.sql.gz` (32M) |
| Timestamp | 2026-08-22 02:30 UTC |
| `gzip -t` | **PASS** |

---

## MySQL RO (iic_booking_ro)

| Check | Result |
|-------|--------|
| Host | `host.docker.internal` |
| Database | `admin` |
| Auth | PASS |
| users / user_wallet / wallet_transactions / booking | **READ OK** |

### Actual column map (production)

| Semantic | Column | Status |
|----------|--------|--------|
| booking_id | `id` | VERIFIED |
| user_id | `user_id` | VERIFIED |
| equipment_id | `equipment_id` | VERIFIED |
| booking_date | `booking_date` | VERIFIED |
| status | `status` | VERIFIED |
| amount | `charge` | VERIFIED |
| employee_id (users) | `emp_id` | VERIFIED |
| start/end time | — | **NOT_FOUND** |
| datetime strategy | — | **BLOCKED: `no_resolvable_datetime_strategy`** |

Production `booking` has **`time_required`** (17 columns total). Operator **column-map file** required before legacy window discovery.

---

## Equipment mapping

**BLOCKED_BY_MIGRATION** — `users.0102` not applied; `LegacyEquipmentMapping` table absent.

---

## Legacy discovery / conflicts

**NOT EXECUTED** — blocked by datetime column map + pending 0102.

---

## Dry-runs (read-only)

| Check | Result |
|-------|--------|
| Test accounts | 1067 users, 218 bookings — **no deletion** |
| Email recipients | Faculty 977, Student 58, OIC 7, Admin 3 — **SMTP sends = 0** |
| Skipped (unsupported roles) | 72 |

---

## Frontend migration UI

**NOT DEPLOYED** — `AdminPortalMigration`, `MigrationPortalBanner`, settlement UI local only.

---

## Phase 8 on production

| Phase | Code | DB schema |
|-------|------|-----------|
| 8A refund/settlement | DEPLOYED | PENDING 0101 |
| 8B bridge/blocks | DEPLOYED | PENDING 0102 |
| 8C notifications | DEPLOYED | PENDING 0103 |

---

## Blockers before migration / T0

1. **Explicit `MIGRATE` approval** required for 0101–0103  
2. **Operator column-map** for `time_required` → start/end datetime  
3. **Equipment mappings** after 0102  
4. **Qualification command bug** — `preview_templates` import error in `migration_production_legacy_qualification`  
5. **Frontend companion release** for migration UX  

---

## Hard stop (honored)

No migrate, T0, blocks, freeze, emails, refunds, or test cleanup.

**Next:** Separate operator approval for `Migrate Production` → then re-run legacy qualification with column-map file.
