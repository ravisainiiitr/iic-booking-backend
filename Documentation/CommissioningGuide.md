# Commissioning Guide — Remote Analysis

## Automatic commissioning

```http
GET|POST /api/v1/analysis/commissioning/run/
```

Produces PASS / WARNING / FAIL for database, cache, reverse tunnel config, fleet heartbeats, duplicates, orphan tunnels, RAW/RESULTS config, Guacamole mock flag, End/Start Analysis routes, and scheduler extensions.

## Manual live workflow

1. Ensure Agent ONLINE (`GET /api/v1/analysis/fleet/inventory/`)
2. Configure equipment RAW/RESULTS + software (`GET /api/v1/analysis/equipment/config-audit/`)
3. Create booking → sample accepted → RAW available
4. Request Analyze Data → expect **awaiting_checkin** (not immediate desktop)
5. Click Start Analysis Session (`POST .../analysis/start/`)
6. Confirm Tunnel ACTIVE + Guacamole desktop
7. End Analysis → collect → S3 → cleanup → email

## Deployment (image rebuild — required)

Do **not** rely on `docker cp`.

```bash
cd /home/ubuntu/iic-booking-backend
docker compose -f docker-compose.production.yml build django celeryworker celerybeat
docker compose -f docker-compose.production.yml up -d django celeryworker celerybeat
docker compose -f docker-compose.production.yml exec django python manage.py migrate --noinput
```

Then re-run commissioning. Containers must survive recreate with code intact.
