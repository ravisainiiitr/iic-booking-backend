# Monitoring Endpoints — Remote Analysis

## Health / probes

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /api/v1/analysis/health/live/` | None | Liveness (process up) |
| `GET /api/v1/analysis/health/ready/` | None | Readiness (DB, cache, Guacamole, enrollment) |
| `GET /api/v1/analysis/health/` | None | Combined + `version` |

Prometheus / LB should scrape **ready** for traffic and **live** for restart decisions.

## Operations (authenticated)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/analysis/operations/toolkit/dashboard/` | Overview + Guacamole probe payload |
| `GET /api/v1/analysis/operations/toolkit/health-report/` | RAG health report |
| `POST /api/v1/analysis/operations/toolkit/connectivity/` | Connectivity suite |
| `GET /api/v1/analysis/operations/diagnostics/` | Deployment diagnostics |
| `GET /api/v1/analysis/session/dashboard/` | Desktop session metrics |
| `GET /api/v1/analysis/operations/dashboard/` | Ops KPIs |

## Celery

| Target | Purpose |
|--------|---------|
| Flower `:5555` (optional profile) | Task monitoring |
| Periodic tasks in Django Admin | Beat registration |

## Agent (on PC)

| URL | Purpose |
|-----|---------|
| `http://127.0.0.1:5088/api/health` | Local agent health (if enabled) |

## Metrics available

| Source | Notes |
|--------|-------|
| Ready JSON fields | `database`, `cache`, `guacamole`, `enrollment`, `status` |
| Toolkit dashboard | Workstation online counts, Guacamole probe |
| Flower (optional) | Celery task rates / failures |
| Docker healthchecks | postgres, redis, django, guacd, guacamole-db, guacamole |
| Agent local health | `5088/api/health` on Analysis PC |

No separate Prometheus `/metrics` exporter is required for RC1; scrape ready/live + Flower as needed.

## Suggested alerts

1. Ready ≠ 200 for &gt; 2 minutes  
2. Guacamole check ≠ `ok` when desktop required  
3. Workstation heartbeat age &gt; 90s  
4. Disk free &lt; 10% on media volume  
5. Celery queue depth / worker down (Flower or broker metrics)  
