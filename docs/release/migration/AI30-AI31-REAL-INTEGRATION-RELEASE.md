# REAL Integration — Staging-Qualified Release Freeze

**Release / version identifier:** `v2.5.2-real-integration-staging-qualified`  
**Freeze commit:** `bd71757` (`release/real-integration-staging-qualified`)  
**Freeze date (UTC):** 2026-08-21  
**Base worktree commit (pre-freeze):** `f7783f9`  
**Production commit (running):** historically `ced49a2` / public API `portal_version=2.5.2` (empty git SHAs)  
**This document authorizes:** git freeze / local commit only  
**This document does NOT authorize:** production deploy, SSH, env edits, migrate, or REAL enablement  
**Push / deploy performed:** NO

---

## Staging qualification (already GO)

| Gate | Result |
|------|--------|
| Channel-I live OAuth / userinfo | PASS / REAL |
| Employee claim | `username` → `admin.users.emp_id` exact match 1 |
| Legacy MySQL RO + wallet/ledger/booking | PASS / REAL |
| Fixture fallback / isolation | NONE / PASS |
| S3 | **LOCAL_STAGING / NOT_AVAILABLE / ACCEPTED FOR STAGING ONLY** (`LOCAL_STAGING_ACCEPTED=true`) |
| Guard tests | 30 PASS |
| Staging REAL activation | GO |

Evidence:

- `AI30-AI31-REAL-STAGING-GO-NO-GO.md`
- `AI30-AI31-OMNIPORT-CALLBACK-FRONTEND-FETCH-DEBUG.md`
- `real_channel_i_live_evidence.json`
- `AI30-AI31-PRODUCTION-MIGRATION-PREFLIGHT.md` → **NOT READY FOR PRODUCTION MIGRATION**

---

## Exact source files in this freeze

### Application / tooling (MUST)

| Path | Role |
|------|------|
| `config/settings/staging.py` | Isolated staging settings; REAL flags; CORS; refuses prod DB/frontend |
| `config/settings/production.py` | Hard-OFF: `REAL_INTEGRATION_ENABLED`, fixtures, `LOCAL_STAGING_ACCEPTED` |
| `docker-compose.staging.yml` | Staging stack (not production compose) |
| `.gitignore` | Ignore `.envs/.staging/` and `.envs/.production/` |
| `config/api_router.py` | Staging fixture auth route wiring |
| `iic_booking/users/api/auth_views.py` | Omniport fixture gate, redirect log redaction, welcome-mail non-blocking |
| `iic_booking/users/api/portal_migration_views.py` | Fixture/REAL status labels for migration UI |
| `iic_booking/users/legacy_ledger/reader.py` | `assert_readonly_sql` enforcement |
| `iic_booking/users/legacy_ledger/snapshot_reader.py` | Refuse fixture under REAL intent |
| `iic_booking/users/legacy_ledger/real_integration_guards.py` | Preflight/status guards |
| `iic_booking/users/legacy_ledger/real_integration_activation.py` | Staging activation orchestration |
| `iic_booking/users/legacy_ledger/real_integration_live_evidence.py` | Durable Channel-I + emp_id re-verify |
| `iic_booking/users/legacy_ledger/booking_lock.py` | Migration booking lock payload (`environment`) |
| `iic_booking/users/identity/channel_i_fixture.py` | Staging fixture Channel-I (blocked when REAL) |
| `iic_booking/users/fixtures/staging_legacy_snapshot.json` | Synthetic fixture data (not production) |
| `iic_booking/users/management/commands/real_integration_status.py` | |
| `iic_booking/users/management/commands/real_integration_preflight.py` | |
| `iic_booking/users/management/commands/real_integration_activate_staging.py` | Staging-only activate |
| `iic_booking/users/tests/test_real_integration_preflight.py` | |
| `iic_booking/users/tests/test_real_integration_activation.py` | |
| `iic_booking/users/tests/test_channel_i_identity_architecture.py` | Identity architecture tests |
| `scripts/staging/provision_staging.sh` | Staging provision with prod-RDS refuse |

### Docs / templates (MUST)

| Path | Role |
|------|------|
| `docs/release/migration/sample.env.staging` | Placeholder-only env template (no real secrets) |
| `docs/release/migration/AI30-AI31-REAL-INTEGRATION-RELEASE.md` | This freeze document |
| `docs/release/migration/AI30-AI31-REAL-STAGING-GO-NO-GO.md` | Staging GO evidence |
| `docs/release/migration/AI30-AI31-PRODUCTION-MIGRATION-PREFLIGHT.md` | Prod preflight NOT READY |
| `docs/release/migration/AI30-AI31-OMNIPORT-CALLBACK-FRONTEND-FETCH-DEBUG.md` | Callback + CORS fix |
| `docs/release/migration/AI30-AI31-REAL-INTEGRATION-OPERATOR-HANDOFF.md` | Operator handoff |
| `docs/release/migration/REAL_INTEGRATION_CREDENTIAL_CHECKLIST.md` | Credential checklist |
| `docs/release/migration/Staging-Channel-I-Verification.md` | Channel-I routing/verification |
| `docs/release/migration/production_migration_preflight.json` | Machine-readable prod preflight |
| `docs/release/migration/real_channel_i_live_evidence.json` | Redacted live evidence (no claim values) |

### Explicitly NOT in this freeze

- `.envs/.staging/.django`, `.envs/.production/.django` (secrets)
- Probe scripts (`_ai29_*`, `_ai30_*`, `_watch_*`, `_export_*`, …)
- PII-heavy JSON dumps (`prod_users_export.json`, wallet mismatch dumps, …)
- Unrelated dirty worktree: `remote_analysis/health.py`, migration `0017`, equipment `0189`, R12 docs, etc.
- Frontend repo changes (separate release if needed)
- Any production deploy workflows or env mutation

---

## Safety properties frozen in code

1. **Production REAL mode cannot auto-enable** — `config/settings/production.py` hard-sets `REAL_INTEGRATION_ENABLED=False`.  
2. **Fixtures hard-off in production** — `CHANNEL_I_STAGING_FIXTURE_MODE=False`, `LEGACY_MYSQL_STAGING_FIXTURE_MODE=False`.  
3. **`LOCAL_STAGING_ACCEPTED` hard-off in production** — cannot carry staging S3 acceptance into production settings.  
4. **Activation/preflight require `DEPLOYMENT_ENVIRONMENT=STAGING`** and refuse RDS host markers.  
5. **MySQL reader** rejects non-SELECT/SHOW/… via `assert_readonly_sql`.  
6. **Employee ID** never falls back to email/name; allow-list claims only; empty claim = BLOCKED.  
7. **Secrets** never printed by status/preflight/activation evidence writers.

---

## Tests

```bash
python manage.py test \
  iic_booking.users.tests.test_real_integration_preflight \
  iic_booking.users.tests.test_real_integration_activation
```

**Expected / required:** 30 PASS

---

## Staging-only S3 limitation

Staging may set `LOCAL_STAGING_ACCEPTED=true` → evidence  
`S3 = NOT_AVAILABLE / ACCEPTED LIMITATION` (not a PASS).

Production must separately prove REAL S3 (or name S3 as a production blocker).  
**Never** set `LOCAL_STAGING_ACCEPTED=true` under production settings (hard-coded `False`).

---

## Production prerequisites (still open)

See `AI30-AI31-PRODUCTION-MIGRATION-PREFLIGHT.md`. Summary:

1. Merge/release this freeze; deploy under change control (separate approval).  
2. Production Channel-I live identity proof (do not blind-copy staging).  
3. Production-scoped MySQL RO + live wallet/ledger/booking RO probes.  
4. Production S3 REAL verification.  
5. Backup timestamps + restore owner.  
6. Read-only `showmigrations` + separately approved migrate.  
7. Explicit operator approval for production migration.

---

## Rollback considerations

- **Git:** revert this release commit / redeploy previous release tag.  
- **Staging:** recreate containers from prior image; fixture modes remain available when REAL is off.  
- **Production:** no production change in this freeze — rollback N/A until a future deploy.  
- **Financial cutover:** still has no simple undo (see Rollback-Runbook); out of scope for this freeze.

---

## Operator commands (staging only)

```bash
# After copying sample.env.staging → .envs/.staging/.django (secrets filled locally, never committed)
docker compose -f docker-compose.staging.yml --env-file .envs/.staging/.django \
  up -d --force-recreate --no-deps django celeryworker celerybeat

docker exec iic-booking-staging-django python manage.py real_integration_status
docker exec iic-booking-staging-django python manage.py real_integration_preflight --json --write-docs
docker exec iic-booking-staging-django python manage.py real_integration_activate_staging --write-docs
```

**Do not run these against production settings.**
