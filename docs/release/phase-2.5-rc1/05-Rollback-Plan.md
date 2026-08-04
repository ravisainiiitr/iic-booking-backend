# Rollback Plan — Platform 2.5.0-rc1

**Goal:** Return to last known-good production (baseline: backend `52ddcfc` / frontend `ffa5af4` on `origin/master` as of 2026-08-04 audit, or newer tagged GA if superseding).

Always take a **database backup** and record image digests **before** upgrading.

---

## Decision tree

| Symptom | First action |
|---------|----------------|
| Frontend 2.5 against broken API | Roll back **Frontend image** only |
| Migration failure mid-deploy | Stop traffic; restore **DB backup**; roll back Backend image |
| Agents incompatible | Keep Portal; publish prior installer; do not force agent upgrade |
| Tunnel/Guacamole only | Roll tunnel/Guacamole config; Portal may stay |

---

## Portal Backend

1. Redeploy previous Docker image digest (from prior manifest).  
2. Do **not** reverse complex RA `0017` on production unless staging proved reverse safe — prefer **DB restore** from pre-upgrade dump.  
3. Confirm `showmigrations` matches rollback baseline.  
4. Restart gunicorn/celery; run `validate_deployment_startup` if available.

## Portal Frontend

1. Redeploy previous frontend image / static artifact.  
2. Clear CDN/browser cache if applicable.  
3. Verify Dashboard no longer requires `/v1/lab/` for core booking.

## Database

1. Stop writers (scale down workers).  
2. Restore Postgres from pre-upgrade backup.  
3. Verify row counts / critical tables smoke.  
4. Bring Backend image matching that schema.

## Docker / compose

1. `docker compose` (or swarm) pin previous tags/digests.  
2. Avoid `latest`.  
3. Record rollback in incident log.

## Configuration

1. Restore `.envs/.production` from config backup (deploy scripts already backup).  
2. Re-apply Guacamole/tunnel secrets from vault — do not invent.

## DSA / Wizard / RAA (agents)

1. Uninstall is last resort; prefer **reinstall prior installer** from Deployment Center previous release row.  
2. Keep ProgramData where upgrade preserved identity.  
3. If pairing/ManagementApiKey policy changed, document re-key steps.

## Reverse Tunnel / Guacamole

1. Revert portal transport mode / gateway URLs to last good settings.  
2. Restart guacd/guacamole; verify health endpoints.  
3. Cancel orphaned sessions per ops runbook.

## Deployment Center metadata

1. Mark bad RC installer rows inactive.  
2. Reactivate prior `is_latest` releases.

---

## Rollback drill (required before GO)

| Step | Owner | Result | Date |
|------|-------|--------|------|
| Backup prod-like staging DB | | | |
| Upgrade staging to RC1 | | | |
| Restore backup + prior images | | | |
| Confirm booking smoke | | | |

---

## Communication

Notify Lab Admins: expected downtime, agent version to use, and when to resume commissioning.
