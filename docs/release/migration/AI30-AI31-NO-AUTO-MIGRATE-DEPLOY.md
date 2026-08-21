# Production deploy without auto-migrate

**Status:** Implementation for follow-up PR (do not amend PR #87 / `6f53b6f`)  
**Policy:** **DEPLOYMENT ≠ MIGRATION**

---

## Root cause

| Path | Command | When | DB write? |
|------|---------|------|-----------|
| `compose/production/django/start` | `manage.py migrate --noinput` | Every Django container start | **YES** |
| `.github/workflows/backend-deploy.yml` | `migrate device_provisioning` | After compose up smoke | **YES** |
| `.github/workflows/backend-deploy.yml` | `POST /api/v1/provisioning/sessions/` | Deploy smoke | **YES** (app write) |
| `scripts/deploy/deploy.sh` | `migrate --noinput` | Scripted deploy | **YES** |
| `scripts/deploy/rollback.sh` | `migrate --noinput` | Scripted rollback | **YES** |
| `.github/workflows/migrate-production.yml` | `migrate --noinput` + provisioning POST | Explicit workflow (was also writing via POST) | **YES** |

Celery worker/beat/flower start scripts: **no migrate**.

---

## Changes in this hotfix

1. Remove migrate from production Django `start` (keep `collectstatic`).
2. Remove migrate + provisioning POST from **Deploy Backend**.
3. Remove migrate from `scripts/deploy/deploy.sh` and `rollback.sh`.
4. Require `confirm_migrate=MIGRATE` on **Migrate Production**; remove provisioning POST smoke.
5. Add `scripts/deploy/migrate-production.sh` (`CONFIRM_MIGRATE=YES`).
6. Add guard tests.

---

## Important: cannot deploy raw `6f53b6f` alone

Merge SHA `6f53b6f` **still contains** auto-migrate in `start`.  
A no-auto-migrate production deploy requires:

```text
6f53b6f  (PR #87 content)
   +
this hotfix commit (merged to master)
```

Then tag/deploy that **combined** tip.

---

## Deploy application (NO migrate) — after this hotfix is on master

1. Create immutable tag on the hotfix merge tip, e.g.  
   `v2.5.2-real-integration-no-automigrate`
2. Optional: run **Backend Release** qualification on that tag.
3. Run **Deploy Backend** with `release_tag=<that tag>`.

Expected: compose build/up, health checks, **no** `migrate`, **no** provisioning POST.

---

## Explicit migration (DO NOT RUN during RO qualification)

**GitHub Actions:** workflow **Migrate Production**  
Input: `confirm_migrate` = `MIGRATE`

**On-host script:**

```bash
CONFIRM_MIGRATE=YES ./scripts/deploy/migrate-production.sh
```

**Manual equivalent (not executed here):**

```bash
docker exec -w /app <production-django> python manage.py showmigrations users
docker exec -w /app <production-django> python manage.py migrate --noinput
```

---

## Application rollback (no DB rollback)

```bash
# Deploy Backend rolls back to previous_release_tag on failure, OR:
./scripts/deploy/rollback.sh
# / ROLLBACK_REF=<previous_sha|tag>
```

Database rollback requires backup restore — **not** equivalent to image rollback.

---

## Production settings hard-OFF (unchanged)

```text
DEPLOYMENT_ENVIRONMENT=PRODUCTION
REAL_INTEGRATION_ENABLED=False
CHANNEL_I_STAGING_FIXTURE_MODE=False
LEGACY_MYSQL_STAGING_FIXTURE_MODE=False
LOCAL_STAGING_ACCEPTED=False
```
