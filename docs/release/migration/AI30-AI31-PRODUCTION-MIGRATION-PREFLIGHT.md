# PRODUCTION MIGRATION PRE-FLIGHT — REAL INTEGRATION

**Verdict:** **NOT READY FOR PRODUCTION MIGRATION**

**Timestamp (UTC):** 2026-08-21  
**Scope:** Pre-flight **only**. No production migration. No production writes. No production REAL enablement. No production env edits. No EC2/RDS modification.

| Safety flag | Value |
|-------------|--------|
| PRODUCTION MIGRATION PERFORMED | **NO** |
| PRODUCTION WRITES | **NO** |
| PRODUCTION EC2 MODIFIED | **NO** |
| PRODUCTION RDS MODIFIED | **NO** |

Machine-readable twin: [`production_migration_preflight.json`](./production_migration_preflight.json)

---

## Staging qualification (input — already GO)

| Gate | Staging |
|------|---------|
| Channel-I live OAuth / userinfo | PASS / REAL |
| Redirect | PASS |
| Employee identity (`username`) | PASS / REAL (emp_id match=1) |
| Legacy MySQL RO + wallet/ledger/booking | PASS / REAL |
| Fixture isolation | PASS |
| Tests | 30 PASS |
| S3 | NOT_AVAILABLE / ACCEPTED LIMITATION (`LOCAL_STAGING_ACCEPTED=true`) |
| Staging REAL activation | GO |

Staging success **does not** authorize production migration.

---

## 1–3. Production architecture & separation

| Component | Finding |
|-----------|---------|
| Public host | `https://equip.iitr.ac.in` |
| EC2 (DNS A) | `3.110.50.174` |
| Django | `docker-compose.production.yml` → image `iic_booking_production_django`, host `8080→5000` |
| Env | `.envs/.production/.django` on EC2 (**gitignored**; **ABSENT** in this local workspace) |
| Frontend | `https://equip.iitr.ac.in` — runtime `VITE_API_URL: '/api'` (same-origin) |
| PostgreSQL | Via production `DATABASE_URL` / RDS (not opened this preflight) |
| Redis | Compose `redis` in production stack |
| Reverse proxy | HTTPS fronting app (deploy docs: Traefik/Apache patterns); CSRF trusts `https://equip.iitr.ac.in` |
| Deploy | GitHub **Deploy Backend** → Linux self-hosted runner on EC2 |
| Staging separation | **PASS** — separate compose (`docker-compose.staging.yml`), DB `iic_booking_staging`, ports `8180/8100`, env `.envs/.staging/` |

---

## 4–6. Channel-I & employee identity (production)

### Channel-I

| Check | Result |
|-------|--------|
| `GET /api/auth/omniport/authorize/` | **PASS** (200, `auth_url` present) |
| Live `redirect_uri` | `https://equip.iitr.ac.in/api/auth/omniport/callback/` |
| Matches required production callback | **YES** |
| Localhost/staging callback | **NO** |
| Client ID/secret values | **NOT PRINTED** (authorize success ⇒ credentials configured) |
| Live production OAuth callback + userinfo | **NOT_TESTED** |
| Evidence class | **NOT_TESTED** |

### Employee identity

| Check | Result |
|-------|--------|
| Staging authoritative claim | `username` |
| Blind copy to production? | **NO** — not done |
| Production `CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM` | **UNKNOWN** (EC2 env not read) |
| Production live emp_id match | **NOT_TESTED** |
| Status | **NOT_VERIFIED** |

Same Channel-I IdP makes `username` the *likely* claim, but production activation requires a **production** identity proof, not staging alone.

---

## 7–10. Legacy MySQL / wallet / ledger / booking

| Check | Result |
|-------|--------|
| Production `OLD_MYSQL_*` presence | **UNKNOWN** (no local production env; EC2 not modified/inspected) |
| Account policy | Must be dedicated **READ-ONLY**; do **not** use `harshit`; do **not** assume staging `iic_booking_ro` is production-scoped |
| Safe reachability this preflight | **NOT_TESTED** |
| Code RO enforcement (`assert_readonly_sql`) | **PASS** (present in repo reader) |
| Live wallet / ledger / booking RO probes on production | **NOT_TESTED** (would require approved RO access; no writes performed) |

---

## 11. Backups & rollback

| Item | Status |
|------|--------|
| Postgres backup mechanism | Documented: `scripts/deploy/backup.sh` (`pg_dump` / `DATABASE_URL`) |
| Latest production backup timestamp | **NOT_VERIFIED** |
| Legacy MySQL backup | **NOT_VERIFIED** |
| Restore procedure | Documented partially (`rollback.sh`, `Documentation/RollbackGuide.md`) |
| Financial/migration rollback | **PARTIAL** — after financial authority, no simple undo (`Rollback-Runbook.md`) |
| This preflight created/deleted backups | **NO** |

---

## 12. Migration state

| Action | Result |
|--------|--------|
| `showmigrations` on production | **NOT_RUN** |
| `migrate` / `makemigrations` | **NOT RUN** |
| Status | **NOT_VERIFIED** |

---

## 13–16. Code / guards / fixtures

| Item | Result |
|------|--------|
| Public `/api/version/` | `portal_version=2.5.2`, `build_date=2026-08-21`, **commits empty** |
| Prior documented prod commit | `ced49a2` |
| Local staging-qualified worktree | backend `f7783f9` (+ uncommitted REAL tooling), frontend `de71188` |
| `real_integration_*.py` + management commands | **Present locally, UNTRACKED in git** — **not** in `HEAD`, **not** in `ced49a2` |
| Guards in production running build | **Almost certainly NO** |
| `REAL_INTEGRATION_ENABLED` | Defined in **staging** settings only; must **not** be enabled on production without separate approval |
| Fixture modes | Staging-only; must stay **false** if ever introduced to a shared settings path |
| Guard tests (code only) | **30 PASS** — no production writes |

**Deploy gap:** Staging REAL GO used code that is **not yet a releasable production artifact**.

---

## 17. Production S3

| Check | Result |
|-------|--------|
| Carry `LOCAL_STAGING_ACCEPTED=true` into production? | **NO** (forbidden) |
| Production `ALLOW_LOCAL_EQUIPMENT_IMAGE_FALLBACK` | **False** (code) |
| Default media URL pattern | S3 URL template in `base.py` |
| Live S3 verify | **NOT_TESTED** |
| Status | **BLOCKED / UNVERIFIED** until REAL S3 proven or named as hard blocker |

---

## 18–20. Callback, CORS, secrets

| Check | Result |
|-------|--------|
| OAuth callback routing | **PASS** — production authorize embeds equip callback only |
| CORS `Origin: https://equip.iitr.ac.in` | **PASS** live (`ACAO=https://equip.iitr.ac.in`) |
| Secrets in Git | **PASS** — `.envs/.production/` gitignored; local production env ABSENT |

---

## 21. Tests

```text
python manage.py test \
  iic_booking.users.tests.test_real_integration_preflight \
  iic_booking.users.tests.test_real_integration_activation
→ 30 PASS
```

Executed against staging Django image / code path. **No production DB writes.**

---

## Gate summary (production)

| Area | Status |
|------|--------|
| Production Channel-I | **PARTIAL** (authorize+redirect PASS; live userinfo **NOT_TESTED**) |
| Production employee identity | **NOT_VERIFIED** |
| Production MySQL | **NOT_VERIFIED** |
| Production wallet | **NOT_TESTED** |
| Production ledger | **NOT_TESTED** |
| Production booking | **NOT_TESTED** |
| Production S3 | **NOT_TESTED / blocker until REAL** |
| Production backups | **NOT_VERIFIED** (mechanism documented) |
| Rollback readiness | **PARTIAL** |
| Production code/version | **2.5.2**; commit SHAs empty; REAL guards **not deployed** |
| Migration readiness | **NOT READY** |
| Security/secret checks | **PASS** (gitignore; no secret print) |
| Production write status | **NO** |

---

## Blockers (must clear before READY)

1. Commit + release the staging-qualified REAL integration tooling; re-qualify; deploy under change control.  
2. Production live Channel-I identity proof (userinfo + claim; expect `username` only after proof).  
3. Production-scoped legacy MySQL RO credentials + live RO wallet/ledger/booking probes.  
4. Production S3 REAL verification (no LOCAL_STAGING acceptance).  
5. Confirm backup timestamps (RDS + MySQL) and named restore owner.  
6. Read-only `showmigrations` review + separately approved migrate plan.  
7. Explicit operator approval for **PRODUCTION MIGRATION** (not granted by this document).

---

## Final result

# NOT READY FOR PRODUCTION MIGRATION

Do **not** proceed beyond pre-flight without explicit operator approval.

```text
PRODUCTION MIGRATION PERFORMED = NO
PRODUCTION WRITES = NO
PRODUCTION EC2 MODIFIED = NO
PRODUCTION RDS MODIFIED = NO
```
