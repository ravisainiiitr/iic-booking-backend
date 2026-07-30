# Production Deployment Guide — Remote Analysis (IIT Roorkee)

**Audience:** System administrators  
**Release:** Remote Analysis v1.0.0-rc1  
**Target:** Fresh server deployable in under **30 minutes** using documented automation.

This guide covers **deployment automation only**. Business logic, APIs, and workflows are unchanged.

## Architecture (compose)

Primary file: [`docker-compose.ra-production.yml`](../../docker-compose.ra-production.yml)

| Service | Role |
|---------|------|
| `django` | Portal (Gunicorn/ASGI) |
| `postgres` | Application database |
| `redis` | Cache + Celery broker |
| `celeryworker` | Async / RA periodic work |
| `celerybeat` | Schedules RA tasks |
| `flower` | Optional (`--profile flower`) |
| `guacamole` + `guacd` + `guacamole-db` | Desktop gateway (`--profile guacamole`) |

Windows **Agent** is installed on Analysis PCs — see [AGENT_INSTALL.md](AGENT_INSTALL.md).

Hardened existing files: `docker-compose.production.yml`, `docker-compose.guacamole.yml` (restart, healthchecks, logging, volumes).

## Fresh install (≤30 minutes)

### 0. Prerequisites

- Docker Engine + Compose v2  
- Git  
- DNS / firewall for Portal HTTPS (and Guacamole HTTPS)  
- Secrets ready (passwords, enrollment key)

### 1. Clone and configure (5 min)

```bash
git clone <repo-url> iic-booking-backend
cd iic-booking-backend
git checkout remote-analysis-v1.0.0-rc1   # or main after tag

mkdir -p .envs/.production
cp docs/release/rc1/sample.env.production .envs/.production/.django
# Edit secrets: SECRET_KEY, POSTGRES_PASSWORD, RA_*, GUACAMOLE_DB_PASSWORD
```

Ensure `.envs/.production/.django` includes at least:

- `DEBUG=False`
- `DJANGO_SECRET_KEY` or `SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS` or `ALLOWED_HOSTS`
- `DATABASE_URL=postgres://iic_booking:<POSTGRES_PASSWORD>@postgres:5432/iic_booking`
- `REDIS_URL=redis://redis:6379/0`
- `CELERY_BROKER_URL=redis://redis:6379/0`
- `POSTGRES_PASSWORD=…` (for compose postgres service)
- `RA_MOCK_GUACAMOLE=false`
- Guacamole + `RA_AGENT_ENROLLMENT_KEY`

### 2. One-command deploy (15–20 min)

```bash
chmod +x deploy.sh rollback.sh verify-production.sh scripts/deploy/*.sh
export COMPOSE_FILE=docker-compose.ra-production.yml
export COMPOSE_PROFILES=guacamole          # add ,flower if needed
export PORTAL_BASE_URL=http://127.0.0.1:8080
./deploy.sh
```

`deploy.sh` will:

1. Backup configuration  
2. `git pull --ff-only` (optional skip)  
3. DB backup  
4. Build images  
5. Start postgres/redis  
6. Migrate + collectstatic + sync RA settings  
7. **Fail-fast** `validate_deployment_startup --strict`  
8. Restart services  
9. Wait for readiness  
10. Run `verify-production.sh`  

### 3. TLS / reverse proxy

Terminate TLS at Traefik/nginx for:

- Portal → container `:8080`  
- Guacamole → container `:8085`  

Set `RA_GUACAMOLE_BASE_URL` to the **public** HTTPS Guacamole URL.

### 4. Agents

Follow [AGENT_INSTALL.md](AGENT_INSTALL.md) on each Analysis PC.

### 5. Accept

```bash
ADMIN_TOKEN=<drf-token> ./verify-production.sh
# Optional deeper:
RUN_CONNECTIVITY=1 RUN_SELF_TEST=1 ADMIN_TOKEN=… ./verify-production.sh
```

Complete [RC1 Production Checklist](../release/rc1/12-Production-Checklist.md).

---

## Upgrade

```bash
export SKIP_GIT_PULL=0
./deploy.sh
```

Uses pre-deploy DB backup unless `SKIP_DB_BACKUP=1`.

## Rollback

```bash
./rollback.sh
# or
ROLLBACK_REF=<previous-sha> ./rollback.sh
```

Restores previous git tree, rebuilds, migrates forward-compatible schema, verifies health.  
If code is older than DB in an incompatible way, restore DB from `backups/deploy/<label>/db/portal.sql.gz` (see DR).

## Disaster recovery

1. Restore postgres from `backups/deploy/.../db/portal.sql.gz`  
2. Restore media from `media/media.tar.gz`  
3. Restore `.envs/.production/.django` from config backup  
4. `./deploy.sh` with `SKIP_GIT_PULL=1` if already on good tag  
5. `./scripts/deploy/restore-verify.sh backups/deploy/<label>`  
6. Re-check agents / Guacamole  

Full narrative: [docs/release/rc1/10-Disaster-Recovery-Guide.md](../release/rc1/10-Disaster-Recovery-Guide.md)

## Startup validator

```bash
./scripts/deploy/validate-startup.sh
# or
docker compose -f docker-compose.ra-production.yml run --rm django \
  python manage.py validate_deployment_startup --strict
```

Checks: secrets, DEBUG, DB, Redis, storage writable, migrations current, Guacamole (unless `--skip-guacamole`).

## Backup automation

```bash
./scripts/deploy/backup.sh
./scripts/deploy/backup.sh --db-only --label nightly
./scripts/deploy/restore-verify.sh backups/deploy/<label>
VERIFY_RESTORE_DB=1 ./scripts/deploy/restore-verify.sh backups/deploy/<label>
```

Schedule nightly via cron.

## Monitoring

See [MONITORING.md](MONITORING.md).

## Known operational issues

| Issue | Mitigation |
|-------|------------|
| Readiness fails on mock Guacamole | Set `RA_MOCK_GUACAMOLE=false` + sync settings |
| `guacd` cannot RDP to PC | Open firewall guacd→PC:3389; check RDP secret |
| Agent not in compose | Expected — install on Windows |
| Flower not running | Start with `COMPOSE_PROFILES=guacamole,flower` |
| Media lost after `compose down -v` | **Never** use `-v` in production |
| First boot migrate slow | Normal; start_period allows 90s |

## Related scripts

| Script | Purpose |
|--------|---------|
| `./deploy.sh` | Full deploy |
| `./rollback.sh` | Git + service rollback |
| `./verify-production.sh` | PASS/FAIL acceptance |
| `scripts/deploy/backup.sh` | DB/media/config backup |
| `scripts/HealthCheck.sh` | Lightweight curl health |

## Compose review summary

| File | Notes |
|------|-------|
| `docker-compose.ra-production.yml` | Full RA stack + profiles, healthchecks, volumes, logging, restart |
| `docker-compose.production.yml` | Hardened redis/django restart + health + logging |
| `docker-compose.guacamole.yml` | Standalone Guac with healthchecks/logging/network |
| `docker-compose.local.yml` | Dev only — not for production |
