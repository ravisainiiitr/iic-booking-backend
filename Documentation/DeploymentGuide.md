# Deployment Guide

Remote Analysis Platform — Release Candidate 1.

## Prerequisites

- PostgreSQL (production) or supported Django DB
- Redis (Celery broker + cache in production)
- Django Portal image (`compose/production/django`)
- Celery worker + beat (+ optional Flower)
- Traefik / TLS termination
- Windows hosts with Remote Analysis Agent installed
- Guacamole (or `mock_guacamole=True` for non-RDP environments only)

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

Agent (`appsettings` / env): `PortalUrl`, agent token after registration, `WorkspaceRoot`, heartbeat/poll intervals.

## Deployment sequence

1. **Backup** database and workspace/archive volumes (see DisasterRecovery.md).
2. **Pull** Portal image / Agent installer for the RC tag.
3. **Migrate:** `python manage.py migrate --noinput` (includes `remote_analysis.0007_*` when present).
4. **Collect static:** `collectstatic --noinput` (production start script does this).
5. **Seed beat:** `post_migrate` registers RA PeriodicTasks — verify in Django Admin → Periodic tasks.
6. **Configure** `RemoteAnalysisSettings` singleton: set Guacamole URLs, **`mock_guacamole=False`**, storage roots, quotas.
7. **Start** django → redis → celeryworker → celerybeat.
8. **Health checks:**  
   - `GET /api/v1/analysis/health/ready/` → 200  
   - `GET /api/v1/analysis/health/live/` → 200  
   - Agent `GET http://localhost:5088/api/health`
9. **Register agents** on each workstation; confirm heartbeats in Portal.
10. **Smoke:** create reservation → session (mock or Guacamole) → workspace upload → ops dashboard → collaboration notifications.
11. **Validate:** `python manage.py validate_remote_analysis`

## Docker Compose

- Local: `docker-compose.local.yml` — django, redis, celeryworker, celerybeat, flower  
- Production: `docker-compose.production.yml` + Traefik  

Recommended Compose healthcheck target: `/api/v1/analysis/health/ready/` on the django service (interval 30s, retries 3).

## Rollback

1. Stop new traffic (Traefik).
2. Redeploy previous image tag.
3. If migration must reverse: only reverse **additive** index migration `0007` if applied; **do not** reverse M1–M7 data migrations without restore from backup.
4. Restore DB backup if schema/data incompatible.
5. Re-run health probes and smoke tests.

## Zero-downtime recommendations

- Run multiple Gunicorn workers behind Traefik.
- Drain Celery workers with warm shutdown (`acks_late` on RA tasks).
- Apply additive index migrations with `CREATE INDEX CONCURRENTLY` equivalent when using PostgreSQL ops outside Django if locks are a concern (optional DBA step).
- Keep Guacamole and Agent rolling updates independent of Portal web deploy.

## Release checklist

- [ ] Backups verified  
- [ ] Migrations applied  
- [ ] `mock_guacamole=False` in production  
- [ ] TLS enabled  
- [ ] Health ready=200  
- [ ] Beat tasks enabled  
- [ ] Agents online  
- [ ] Smoke tests passed  
- [ ] `validate_remote_analysis` OK  
