# Deployment Guide

Remote Analysis Platform — Release Candidate 1.

**Canonical production automation:** [docs/deploy/Production-Deployment-Guide.md](../docs/deploy/Production-Deployment-Guide.md)  
**IIT Roorkee ops runbook:** [docs/deploy/Operations-Runbook-IITR.md](../docs/deploy/Operations-Runbook-IITR.md)  
**Monitoring:** [docs/deploy/MONITORING.md](../docs/deploy/MONITORING.md)

## Prerequisites

- PostgreSQL (production) or supported Django DB
- Redis (Celery broker + cache in production)
- Django Portal image (`compose/production/django`)
- Celery worker + beat (+ optional Flower)
- Traefik / TLS termination
- Windows hosts with Remote Analysis Agent installed
- Guacamole (or `mock_guacamole=True` for non-RDP environments only)

## One-command deploy

```bash
./deploy.sh
./verify-production.sh
./rollback.sh   # if needed
```

Compose: `docker-compose.ra-production.yml` (Portal + Postgres + Redis + Celery + Guacamole profiles).

## Environment variables (Portal)

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` / Django DB settings | Primary database |
| `REDIS_URL` | Celery + production cache |
| `DJANGO_SECRET_KEY` | Django secret |
| `DJANGO_ALLOWED_HOSTS` | Host allowlist |
| `DJANGO_SECURE_*` / Traefik | HTTPS assumptions |
| `WEB_CONCURRENCY` | Gunicorn workers (default 4) |
| `GUNICORN_TIMEOUT` | Default 120s (large uploads) |
| `RA_*` | Remote Analysis / Guacamole overlays (see RC1 config audit) |

Agent (`appsettings`): `PortalBaseUrl`, `SessionWorkspaceRoot`, `LocalHealthPort` (default 5088), heartbeat/poll intervals, HTTP retries. Token persisted under `ProgramData/.../State` after registration.

## Deployment sequence

1. **Backup** database and workspace/archive volumes (`./scripts/deploy/backup.sh`).
2. **Pull** Portal image / Agent installer for the RC tag (`./deploy.sh` or manual).
3. **Migrate:** `python manage.py migrate --noinput` (through `remote_analysis.0012_*`).
4. **Collect static:** `collectstatic --noinput` (production start script does this).
5. **Seed beat:** `post_migrate` registers RA PeriodicTasks — verify in Django Admin → Periodic tasks.
6. **Validate:** `python manage.py validate_deployment_startup --strict`
7. **Configure** `RemoteAnalysisSettings`: Guacamole URLs, **`mock_guacamole=False`**, storage roots, quotas (`sync_remote_analysis_settings`).
8. **Start** django → redis → celeryworker → celerybeat (+ guacamole profile).
9. **Health checks:**  
   - `GET /api/v1/analysis/health/ready/` → 200  
   - `GET /api/v1/analysis/health/live/` → 200  
   - Agent `GET http://localhost:5088/api/health`
10. **Register agents** on each workstation; confirm heartbeats in Portal.
11. **Smoke:** `./verify-production.sh` (+ optional toolkit connectivity/self-test).
12. **Architecture validate:** `python manage.py validate_remote_analysis`

## Docker Compose

- **RA production (recommended):** `docker-compose.ra-production.yml`  
- Local: `docker-compose.local.yml` — django, redis, celeryworker, celerybeat, flower  
- Legacy production: `docker-compose.production.yml` + Traefik  
- Guacamole standalone: `docker-compose.guacamole.yml`

Production Compose includes django `healthcheck` on `/api/v1/analysis/health/ready/`, redis healthcheck, restart policies, and log rotation.

## Rollback

```bash
./rollback.sh
```

1. Stop new traffic (Traefik) if needed.
2. Redeploy previous image/git tag via rollback script.
3. Prefer **application rollback**; restore DB only if schema incompatible.
4. Re-run `./verify-production.sh`.

## Zero-downtime recommendations

- Run multiple Gunicorn workers behind Traefik.
- Drain Celery workers with warm shutdown (`acks_late` on RA tasks).
- Apply additive index migrations carefully under load (optional DBA step).
- Keep Guacamole and Agent rolling updates independent of Portal web deploy.

## Release checklist

- [ ] Backups verified  
- [ ] Migrations applied through 0012  
- [ ] `mock_guacamole=False` in production  
- [ ] TLS enabled  
- [ ] Health ready=200  
- [ ] Beat tasks enabled  
- [ ] Agents online  
- [ ] `verify-production.sh` PASS  
- [ ] `validate_deployment_startup --strict` OK  
