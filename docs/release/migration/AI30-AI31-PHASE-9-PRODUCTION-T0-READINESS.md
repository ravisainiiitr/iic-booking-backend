# Phase 9 — Production Migration T0 Readiness

**Audit date:** 2026-08-22  
**Mode:** READ-ONLY / PREPARATION ONLY  
**Production host:** `equip.iitr.ac.in`  
**Verdict:** **PRODUCTION MIGRATION BLOCKED — DO NOT PROCEED**

No production T0 was activated. No production writes were performed during this phase.

---

## Executive summary

Production baseline health, backup, Channel-I identity, MySQL RO, and migrations **0096–0100** remain **PASS** per the approved post-release audit (`production_post_release_audit.json`, workflow run `32509466243`).

Production is **not** ready for migration T0 because:

1. **Phase 8A/8B/8C code is not deployed** — production remains at SHA `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea`; repo readiness tooling and bridge logic are at `81c012bc9b8a45c3cdfee5d412689fce1dd292d0`+.
2. **`users.0101`–`0103` are not applied** on production (separate gated deploy required).
3. **Equipment mapping / legacy blocks / notification tables do not exist** on production until `0102`/`0103`.
4. **Upcoming-week legacy booking discovery** has not been executed with an operator-verified MySQL column map.
5. **Host-side RO audits** (user counts, email recipient classification, test-account dry-run) were not executed on the production container during this phase.

Even after all gates pass, **explicit operator GO** is required before T0. Passing readiness ≠ authorization to migrate.

---

## 9.1 — Current production release

| Item | Value |
|------|-------|
| **Production SHA** | `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea` |
| **Production tag** | `v2.5.2-channel-i-user-savepoint` |
| **Portal/backend/frontend version** | `2.5.2` |
| **Build date (`/api/version/`)** | `2026-08-22` |
| **Deployment timestamp** | Post-release audit: 2026-08-21 (workflow `32509466243`); public version API refreshed 2026-08-22 |
| **Docker image** | Production compose stack — exact digest via host `docker inspect` (RO) |
| **Phase 8 code SHA (repo, not deployed)** | `81c012bc9b8a45c3cdfee5d412689fce1dd292d0` |

### Health endpoints (live, read-only)

| Endpoint | Result |
|----------|--------|
| `GET /api/version/` | **200 PASS** — versions 2.5.2 |
| `GET /api/v1/analysis/health/ready/` | **200 PASS** — database ok, cache ok |
| `GET /api/health/` | **404** — not exposed; use analysis readiness |

### Hard-OFF / production safety (post-release audit)

| Check | Result |
|-------|--------|
| `DEPLOYMENT_ENVIRONMENT=PRODUCTION` | PASS |
| `CHANNEL_I_STAGING_FIXTURE_MODE` | false |
| `LEGACY_MYSQL_STAGING_FIXTURE_MODE` | false |
| `LOCAL_STAGING_ACCEPTED` | false |
| `DEBUG` | false |
| Channel-I REAL integration | PASS (claim = username) |
| Startup auto-migrate | **forbidden** (`/start` has no migrate) |

---

## 9.2 — Migration state (READ ONLY)

**Source:** `production_post_release_audit.json` + approved workflow `.github/workflows/show-production-migrations.yml`  
**`migrate` was NOT run during Phase 9.**

| Migration | Production |
|-----------|------------|
| users.0096 | **[X] APPLIED** |
| users.0097 | **[X] APPLIED** |
| users.0098 | **[X] APPLIED** |
| users.0099 | **[X] APPLIED** |
| users.0100 | **[X] APPLIED** |
| users.0101 | **[ ] NOT APPLIED** — Phase 8A settlement schema |
| users.0102 | **[ ] NOT APPLIED** — Phase 8B equipment/block bridge |
| users.0103 | **[ ] NOT APPLIED** — Phase 8C notification batch |
| equipment.0188 | **NOT APPLIED** |
| R14 migration | **NOT APPLIED** |

**Gate:** `0101`–`0103` require **separate production migration approval** before T0. Do not apply them during this readiness phase.

---

## 9.3 — Production backup

| Item | Result |
|------|--------|
| **Backup** | **PASS** |
| Latest nightly | `nightly-20260821` |
| Artifact | `/home/ubuntu/backups/nightly/nightly-20260821/db/portal.sql.gz` |
| gzip integrity | PASS |
| Scripts | `backup.sh`, `rollback.sh`, `restore-verify.sh` present |
| Restore verification | Script present; full restore **not** executed |

**T-1 requirement:** Re-verify a backup taken **immediately before** the planned T0 window.

---

## 9.4 — Production user population

**Status:** NOT EXECUTED on production host during Phase 9 (no DB shell writes).

When executed via read-only command on host:

```bash
docker exec -w /app <django-cid> python manage.py migration_production_t0_readiness
```

Report **counts only** (no email, employee ID, name, or phone):

| Role bucket | Count |
|-------------|------:|
| Faculty | PENDING |
| Student (incl. individual) | PENDING |
| OIC / Manager | PENDING |
| Main Administrator | PENDING |
| Lab-in-Charge (`operator`) | PENDING |
| Department Admin | PENDING |
| Normal / external / other | PENDING |
| Unsupported / ambiguous | PENDING |
| `is_test_account=True` | PENDING |
| **Total active** | PENDING |

### Email template mapping (code policy)

| Application role | Template | Policy |
|------------------|----------|--------|
| Faculty | `FACULTY_MIGRATION` | Auto batch |
| Student / Individual Student | `STUDENT_MIGRATION` | Auto batch |
| OIC (`manager`) | `OIC_MIGRATION` | Auto batch |
| Main Administrator (`admin`) | `ADMIN_MIGRATION` | Auto batch |
| Lab-in-Charge (`operator`) | **None** | Manual operational briefing — **do not** assign Faculty/Admin template |
| Department Admin (`dept_admin`) | **None** | Manual operational briefing |
| Normal User / external types | **None** | Excluded from blast; old-portal redirect + in-app messaging at T0 |

Implementation: `classify_migration_template()` in `users/legacy_ledger/migration_emails.py`.

---

## 9.5 — Migration email recipient safety

**Code verification:** PASS  
**Production dry-run execution:** NOT RUN (requires host + `0103` schema)

Verified in code (`migration_notifications.py`):

- One batch per T0 orchestration
- Recipient classification via `select_notification_candidates`
- Async Celery delivery (`send_migration_notification_recipient`)
- Idempotent SENT skip; FAILED retry
- Statuses: PENDING / QUEUED / SENT / FAILED / SKIPPED
- Production live send **blocked** in Phase 8C tools (`_deployment_is_production()` guards)
- Emails must not send before T0 + freeze + blocks

**Phase 9 audit path:** use `select_notification_candidates()` only — **never** `create_notification_batch()` on production audit (creates DB rows).

**Expected emails sent during Phase 9:** **ZERO**

---

## 9.6 — Upcoming-week legacy booking discovery

**Status:** NOT EXECUTED on production

| Metric | Value |
|--------|------:|
| Total legacy bookings | — |
| Eligible | — |
| Cancelled | — |
| Completed | — |
| Outside window / invalid | — |
| Unmapped equipment | — |
| Conflicting | — |
| Settlement/refund required | — |

**Reason:** Live MySQL booking column map is **not hard-coded** (`booking_bridge.discover_legacy_bookings` schema note). Operator must supply a verified normalized JSON export:

```bash
python manage.py migration_production_t0_readiness --legacy-rows-file /secure/legacy_upcoming_week.json
```

**No blocks, copies, or slot modifications** were performed.

---

## 9.7 — Equipment mapping audit

**Status:** **BLOCKED**

| Check | Result |
|-------|--------|
| `LegacyEquipmentMapping` table | **Absent** (users.0102 not applied) |
| Mapped count | 0 |
| Unmapped / conflict | Cannot audit |
| Fuzzy matching | Not used (by design) |
| Auto-create mappings | Not performed |

After `0102` + explicit mapping load, run:

```bash
python manage.py validate_legacy_equipment_mapping
```

**Gate:** 100% mapping for every legacy equipment ID referenced by eligible upcoming bookings.

---

## 9.8 — Slot availability audit

**Status:** NOT EXECUTED (depends on 9.6 + 9.7)

Hybrid protection (code verified, not activated on prod):

- `LegacyBookingBlock` (audit)
- `DailySlot.status=BLOCKED`, label `LEGACY_MIGRATION:{id}`
- New booking overlap → **409 `LEGACY_MIGRATION_SLOT_BLOCKED`** (`equipment/api_views.py`)

| Metric | Value |
|--------|------:|
| Eligible legacy bookings | — |
| Corresponding new slots | — |
| Already occupied | — |
| Conflicts | — |
| Blockable at T0 | — |

---

## 9.9 — Old portal booking freeze policy

**Code verification:** PASS (not activated)

`legacy_portal_mutating_booking_blocked()` blocks create/reschedule/waitlist/sample when `booking_migration_mode` ∈ `{FREEZE, ACTIVE, SETTLEMENT, COMPLETED}`.

| Role | Expected at T0 | Code signal |
|------|----------------|-------------|
| Normal User / Faculty | No new booking; view permitted; redirect to new portal | `MIGRATION_BOOKING_DISABLED` |
| OIC | No new booking; legacy ops + migration refund | Same gate + OIC refund RBAC |
| Lab-in-Charge / Dept Admin | No new booking; operational visibility | Gate applies to mutating booking actions |
| Main Administrator | No new booking; global visibility + refund | Gate + admin global APIs |

Banner text (code): *"IIC Booking has migrated to the new portal…"*

**Production freeze:** **NOT activated** (`booking_migration_mode` remains NORMAL).

---

## 9.10 — Main Administrator global view

**Code verification:** PASS

Main Admin APIs (`portal_legacy_bridge_views.py`):

- All departments equipment mappings (`qs.all()` — no department switch)
- All migration batches / legacy blocks
- Settlement / reconciliation visibility
- Server-side `_is_main_admin()` RBAC

**No additional permissions granted during Phase 9.**

---

## 9.11 — Migration refund policy

**Code verification:** PASS — **no production refunds issued**

| Actor | Refund | Scope |
|-------|--------|-------|
| OIC (`manager`) | YES | Equipment assignment scope only |
| Main Administrator | YES | All departments |
| All others | NO | `can_issue_migration_refund()` returns false |

Mechanics:

- One-time `MigrationBookingSettlement` per booking (unique constraint)
- Ledger credit via `SubWallet.credit()` — never direct balance mutation
- Reason required; FAILED stays FAILED; COMPLETED is idempotent-rejected
- Does not free slots or unlock end-user booking

---

## 9.12 — Test account cleanup (dry-run only)

**Status:** NOT EXECUTED on production host

Command (dry-run default — **no deletion**):

```bash
python manage.py migration_cleanup_test_accounts --dry-run
```

Safety rules verified in code:

- Selects **only** `is_test_account=True`
- Never classifies by email/name patterns
- Blocks if real user count would change

| Metric | Value |
|--------|------:|
| Test users | PENDING |
| Test bookings | PENDING |
| Non-test selected | Must be 0 |

---

## 9.13 — Approved production T0 sequence

1. **T-1** — Final backup verification  
2. Test-account cleanup (`--confirm-test-cleanup`)  
3. Equipment mapping validation (100% READY)  
4. Upcoming booking discovery (verified column map)  
5. `migration_dry_run` → **READY FOR MIGRATION**  
6. **Operator GO confirmation**  
7. **T0** — activate migration state  
8. Freeze OLD portal booking creation  
9. Create NEW portal legacy blocks (+ DailySlot BLOCKED)  
10. Reconcile blocks  
11. Verify NEW portal availability  
12. Create migration notification batch  
13. Queue migration emails (**after** freeze + blocks verified)  
14. Monitor exceptions  

**Ordering rules:**

- Do **not** send emails before old-portal freeze is active  
- Do **not** allow conflicting NEW bookings until legacy blocks are verified  

Production orchestration command for staging reference only: `migration_staging_t0` — **refuses PRODUCTION**.

---

## 9.14 — Atomicity / failure safety

| Operation | Classification |
|-----------|----------------|
| Per-booking `arm_legacy_block` | Transactional |
| Staging T0 outer batch | Transactional (`transaction.atomic`) |
| Email queue | Retryable (FAILED recipients) |
| ACTIVE blocks | Compensatable via `migration_abort_batch` |
| Completed refunds | **Irreversible** |
| SMTP SENT emails | **Irreversible** |

Safeguards:

- No partial silent migration — batch status + counts audited  
- Duplicate active block guard  
- Abort before irreversible financial settlement  
- Notification batch auditable (`MigrationNotificationBatch` / `MigrationNotificationRecipient`)  

---

## 9.15 — Old portal redirect message

**Code verification:** PASS

Normal user / Faculty message includes migration notice and new portal CTA. Mutations blocked with `MIGRATION_BOOKING_DISABLED`. OIC/Admin operational paths remain as designed.

---

## 9.16 — Production email preview

**Code verification:** PASS (no send)

Templates verified in repo:

- Faculty, Student, OIC, Admin  
- Navy branding `#1D2844`  
- Responsive table layout  
- CTA: "Access New IIC Booking Portal"  
- Support details, migration timing placeholders  
- Preview command: `migration_email_preview`  

Production URL must be `https://equip.iitr.ac.in` (or configured `new_portal_url`) — no localhost/staging URLs in production context.

---

## 9.17 — Rollback / abort plans

| Plan | Action |
|------|--------|
| **Abort before settlement** | `migration_abort_batch` — release ACTIVE blocks, retain audit |
| **App rollback** | Redeploy prior tag `v2.5.40-r13-ghost-reserved` (documented) |
| **DB rollback** | Requires restore from pre-T0 backup — **not executed** |
| **Email abort** | Do not queue; dry-run batches marked DRY_RUN |

Prior backup: `nightly-20260821` (verify fresher backup at T-1).

---

## 9.18 — GO / NO-GO checklist

| Gate | Status |
|------|--------|
| Production health PASS | ✅ |
| Backup PASS | ✅ |
| Migrations 0096–0100 PASS | ✅ |
| 0101–0103 deployment plan separately approved | ❌ Not applied |
| No unexpected migrations (0188/R14) | ✅ |
| Equipment mappings 100% complete | ❌ Blocked (0102) |
| Upcoming eligible legacy bookings 100% resolvable | ❌ Not executed |
| Zero unmapped eligible bookings | ❌ |
| Zero unresolved slot conflicts | ❌ |
| Test cleanup dry-run safe | ❌ Not executed on prod |
| Role classification complete | ❌ Not executed on prod |
| Email templates verified | ✅ (code) |
| Email dry-run = ZERO sends | ✅ (none sent) |
| Old portal freeze enforcement verified | ✅ (code) |
| New portal conflict blocking verified | ✅ (code) |
| OIC refund scope verified | ✅ (code) |
| Main Admin global view verified | ✅ (code) |
| Abort/reconciliation path verified | ✅ (code) |
| Production hard-OFF protections intact | ✅ |
| No production writes during Phase 9 | ✅ |

---

## Production writes during Phase 9

**None.** Confirmed:

- No T0 activation  
- No `booking_migration_mode=ACTIVE`  
- No legacy blocks / batches  
- No migrate / 0101–0103  
- No emails / refunds / test deletions  
- No schema or MySQL mutations  

---

## Operator next steps (before re-audit)

1. **Approve and deploy** `users.0101`–`0103` via separate gated production migration workflow.  
2. **Deploy** Phase 8A/8B/8C application code to production (tag + SHA recorded).  
3. **Load** explicit `LegacyEquipmentMapping` rows (no fuzzy match).  
4. **Export** upcoming-week legacy bookings with operator-verified MySQL column map.  
5. **Run on production host (RO):**  
   ```bash
   python manage.py migration_production_t0_readiness \
     --legacy-rows-file /secure/legacy_upcoming_week.json \
     --json-out /tmp/phase9_readiness.json
   python manage.py migration_cleanup_test_accounts --dry-run
   ```  
6. Re-run GO/NO-GO with populated counts.  
7. Obtain **explicit operator GO** before T0.  

---

## Final output (Phase 9.18)

| # | Item | Result |
|---|------|--------|
| 1 | Production SHA | `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea` |
| 2 | Production tag | `v2.5.2-channel-i-user-savepoint` |
| 3 | Health | **PASS** |
| 4 | Migration state | **0096–0100 applied; 0101–0103 pending** |
| 5 | Backup | **PASS** |
| 6 | User counts by role | **PENDING host RO** |
| 7 | Equipment mapping | **BLOCKED** (0102 not applied) |
| 8 | Upcoming legacy booking count | **NOT EXECUTED** |
| 9 | Unmapped booking count | **NOT EXECUTED** |
| 10 | Slot conflict count | **NOT EXECUTED** |
| 11 | Test-account dry-run | **NOT EXECUTED on prod** |
| 12 | Email recipient dry-run | **NOT EXECUTED on prod; 0 sends in Phase 9** |
| 13 | Email template verification | **PASS (code)** |
| 14 | Old portal freeze verification | **PASS (code; not activated)** |
| 15 | New portal blocking verification | **PASS (code; not activated)** |
| 16 | OIC refund authorization | **PASS (code)** |
| 17 | Main Administrator global view | **PASS (code)** |
| 18 | Reconciliation/abort readiness | **PASS (code)** |
| 19 | Production writes during phase | **ZERO** |

### Verdict

# PRODUCTION MIGRATION BLOCKED — DO NOT PROCEED

Await resolution of blockers, host-side RO audit with populated counts, Phase 8 schema deploy approval, and **explicit operator GO** before any T0 activation.
