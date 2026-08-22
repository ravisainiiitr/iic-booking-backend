# AI30–AI31 Production Post-Release Audit

**Updated (UTC):** 2026-08-21T17:42Z  
**Verdict:** **POST-RELEASE AUDIT = PASS**

```text
AUDIT_MODE = READ-ONLY
PRODUCTION_MODIFICATIONS = NONE
MIGRATE = NOT RUN
REDEPLOY = NOT RUN
SHA_DRIFT = NO
SECRET_IN_LOGS = NO
MYSQL_WRITABLE = FALSE
```

---

## 1. Release baseline

| Field | Result |
|-------|--------|
| Production SHA | `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea` |
| Production tag | `v2.5.2-channel-i-user-savepoint` |
| `sha_ok` | **YES** |
| Annotated tag object | peels to same commit (`7d1081d…`) |
| `/api/version/` | HTTP 200 — portal/backend/frontend `2.5.2`, build_date `2026-08-21` |
| Compose in use | `docker-compose.production.yml` |

---

## 2. Migration immutability

| Check | Result |
|-------|--------|
| users.0096–0100 | all **[X]** |
| Pending (any app) | **NONE** |
| equipment.0188 | **NOT APPLIED** |
| R14 | **NOT APPLIED** |
| `migrate` / `makemigrations` this phase | **NOT RUN** |

---

## 3. Production environment safety (effective values)

| Setting | Effective value |
|---------|-----------------|
| `DEPLOYMENT_ENVIRONMENT` | `PRODUCTION` |
| `REAL_INTEGRATION_ENABLED` | `False` (intentional hard-off) |
| `CHANNEL_I_STAGING_FIXTURE_MODE` | `False` |
| `LEGACY_MYSQL_STAGING_FIXTURE_MODE` | `False` |
| `LOCAL_STAGING_ACCEPTED` | `False` |
| `DEBUG` | `False` |
| `channel_i_fixture_mode_enabled()` | `False` |
| `CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM` | `username` |
| `DJANGO_SETTINGS_MODULE` | production module (container) |

**Conclusion:** Staging fixture mechanisms cannot activate with current production flags.

---

## 4. Channel-I health

| Check | Result |
|-------|--------|
| Authorize | **PASS** (REAL) |
| `fixture_mode` | `false` |
| Evidence class | **REAL** |
| Callback endpoint | **PASS** (HTTP 302 without code = validation behavior) |
| Claim | **username** (unchanged) |
| Interactive re-login | **NOT required** this audit |

No OAuth tokens/secrets printed.

---

## 5. Durable identity

| Check | Result |
|-------|--------|
| Table present | **YES** |
| Row count | **1** (≥ 1) |
| Has username / user FK / sync timestamp | **YES** (no PII printed) |
| Employee match count | **1** |
| Manual profile mutation this phase | **NO** |

---

## 6. Legacy MySQL RO

| Check | Result |
|-------|--------|
| Host / port | `host.docker.internal` / `3306` |
| Database | `admin` |
| User | `iic_booking_ro` |
| users / wallet / ledger / booking reads | **PASS** |
| Grants | `USAGE` + **`SELECT ON admin.*`** (`iic_booking_ro@172.18.%`) |
| `account_appears_writable` | **FALSE** |
| MySQL RO overall | **PASS** |

No UPDATE/DELETE probes performed.

---

## 7. Application health

| Component | Result |
|-----------|--------|
| Django container | healthy |
| Readiness (local + public) | **200** |
| `/api/version/` | **200** |
| Authorize GET/OPTIONS | **200** |
| Frontend `/` | **200** |
| Celery worker / beat | Up |
| Redis | healthy |
| Frontend | healthy |

---

## 8. S3

| Check | Result |
|-------|--------|
| Storage backend | `storages.backends.s3boto3.S3Boto3Storage` |
| Bucket configured | **YES** |
| Read-only `list_objects_v2` (MaxKeys=1) | **PASS** |
| Uploads/deletes/policy changes | **NOT performed** |

---

## 9. Backup verification

| Check | Result |
|-------|--------|
| Newest nightly | `nightly-20260821` |
| Artifact | `db/portal.sql.gz` |
| `gzip -t` | **PASS** |
| `backup.sh` / `rollback.sh` / `restore-verify.sh` | **PRESENT** |
| Restore of production | **NOT performed** |
| Isolated non-prod restore drill | **NOT run** this phase (optional, separate) |

---

## 10. Log / error audit

| Signal (recent tails) | Observation |
|-----------------------|-------------|
| Django ERROR/CRITICAL/Traceback (last ~400 lines) | **4** matches |
| MySQL auth failures | **0** |
| Celery ERROR/CRITICAL/Traceback (last ~200) | **0** |
| Redis ERROR/CRIT (last ~100) | **0** |
| OAuth/Channel-I keyword noise | elevated mention count (includes routine Omniport traffic); not treated as incident alone |
| **Secrets in logs** | **NO** (`SECRET_IN_LOGS=NO`) |

**Classification:** No critical production incident identified from sanitized tails. Expected callback-without-code validation and routine auth traffic may contribute to keyword matches. Continue monitoring Channel-I callback failures and wallet identity errors.

---

## 11. Docker / service / auto-migrate

| Check | Result |
|-------|--------|
| Production images | `iic_booking_production_*` |
| Django cmd | `["/start"]` |
| `/start` auto-migrate | **ABSENT** — comments explicitly forbid `manage.py migrate` |
| Local `docker-compose.local.yml` on host | file may exist; **not** the running production stack |
| Fixture / local staging storage mode | **OFF** |

`scripts/deploy/deploy.sh` remains **DEPLOYMENT ≠ MIGRATION**.

---

## 12. Rollback readiness

| Check | Result |
|-------|--------|
| Current release tag | `v2.5.2-channel-i-user-savepoint` → `7d1081d…` |
| Documented prior baseline tag | `v2.5.40-r13-ghost-reserved` **exists** |
| Tag commit | `20321ff5a2d77a498951a7866f1514c45cb2e490` (merge of R13 hotfix) |
| Documented SHA `ced49a24…` | **exists as commit**; is **not** the peeled tip of the tag (hygiene note — use tag or host `previous_git_ref`) |
| `rollback.sh` | **PRESENT**; states **NO migrate** / DB not rolled back by app rollback |
| Docs | `AI30-AI31-NO-AUTO-MIGRATE-DEPLOY.md`, `Documentation/RollbackGuide.md` |
| Rollback executed this phase | **NO** |

**Principle preserved:** application rollback ≠ database rollback. DB recovery = backup restore.

---

## 13. Git / release hygiene

| Check | Result |
|-------|--------|
| Release tag exists on origin | **YES** (annotated → `7d1081d…`) |
| `.env` tracked | **NO** (gitignored) |
| Committed env samples | `docs/release/rc1/sample.env.production` only (sample) |
| Production secrets pushed this audit | **NO** |
| Repo pushes this audit | probe workflow only (documentation/audit CI); **no production redeploy** |

---

## 14. Monitoring checklist (recommendations)

| Signal | Suggested check | Suggested threshold / action |
|--------|-----------------|------------------------------|
| API readiness | `GET /api/v1/analysis/health/ready/` | alert if ≠ 200 for >2 min |
| Channel-I authorize | `GET /api/auth/omniport/authorize/` returns `auth_url`, `fixture_mode=false` | alert if 5xx or fixture true |
| Channel-I callback failures | app logs / 5xx on callback | alert on spike vs baseline |
| Identity mismatch | match count ≠ 1 for staff logins | page onboarding owner |
| MySQL RO connectivity | OldMySQL reader health / ERROR rate | alert on auth/connect errors |
| Wallet / booking reads | RO reader failures | alert on sustained errors |
| S3 | list/get error rate | alert on AccessDenied / 5xx |
| Celery | worker/beat up; queue lag | alert if worker down >2 min |
| Redis | container healthy | alert if unhealthy |
| Backup freshness | nightly `portal.sql.gz` mtime | alert if age > 36h |
| Disk usage | `/` and backup volume | alert >85% |
| Container health | django/frontend/redis | alert on restart loop |
| SHA drift | `git rev-parse HEAD` on deploy host | alert if ≠ known-good baseline |

Do **not** auto-install new monitoring stack; wire into existing alerts where available.

---

## 15. Production safety summary

| Gate | Status |
|------|--------|
| SHA baseline | **PASS** |
| Migrations immutable | **PASS** |
| Env / fixture safety | **PASS** |
| Channel-I REAL | **PASS** |
| Durable identity | **PASS** |
| MySQL SELECT-only | **PASS** |
| Wallet / ledger / booking RO | **PASS** |
| S3 RO | **PASS** |
| Backup | **PASS** |
| Health | **PASS** |
| Auto-migrate absent | **PASS** |
| Secrets in logs | **PASS** (none found) |
| Rollback docs/scripts | **PASS** (with tag/SHA hygiene note) |
| **POST-RELEASE AUDIT** | **PASS** |

### Production modifications performed

**NONE** (read-only audit only).

---

## Files

- `docs/release/migration/AI30-AI31-PRODUCTION-POST-RELEASE-AUDIT.md` (this file)
- `docs/release/migration/production_post_release_audit.json`
