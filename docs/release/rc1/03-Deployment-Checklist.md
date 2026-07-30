# Remote Analysis RC1 — Deployment Checklist

## Supported topology

```
Users (HTTPS)
    → Portal (Django + Gunicorn/uWSGI)
        → PostgreSQL
        → Redis
        → Celery worker + Celery beat
    → Guacamole (HTTPS public) → guacd → Analysis PC RDP
Analysis PC
    → Remote Analysis Agent → Portal HTTPS (heartbeat/commands/workspace)
```

## Automated path (preferred)

```bash
export COMPOSE_FILE=docker-compose.ra-production.yml
export COMPOSE_PROFILES=guacamole
./deploy.sh
./verify-production.sh
```

See [Production Deployment Guide](../../deploy/Production-Deployment-Guide.md).

## Deployment order

| Step | Component | Validation |
|------|-----------|------------|
| 1 | PostgreSQL | Accept connections; backups configured |
| 2 | Redis | `PING` OK |
| 3 | Portal code + env | `DEBUG=False`; secrets set |
| 4 | `migrate` (through `remote_analysis.0012`) | `showmigrations remote_analysis` all `[X]` |
| 5 | `sync_remote_analysis_settings` | `mock_guacamole=False` |
| 6 | Portal web process | `/api/v1/analysis/health/live/` → ok |
| 7 | Celery worker | Consumes RA queues/tasks |
| 8 | Celery beat | Periodic tasks registered (see `signals.py`) |
| 9 | Readiness | `/api/v1/analysis/health/ready/` → `ready` (Guac ok, enrollment configured) |
| 10 | Guacamole + guacd + Guac DB | Admin login; compose or equiv |
| 11 | Network: guacd → Analysis PC:3389 | Firewall allow |
| 12 | Agent install + enroll | Heartbeat age &lt; 90s; status AVAILABLE |
| 13 | WorkstationRdpSecret | Set per PC |
| 14 | Smoke | Toolkit Guacamole tab; booking desktop launcher (mock off) |
| 15 | Commissioning | SAT-05 sync path on first PC; then Guac SAT-11 as required |

## Do not

- Start Agents before Portal readiness is green  
- Enable Guacamole with empty API URL (falls back to mock)  
- Run Celery beat on multiple nodes without a single-leader strategy  

## Rollback of a bad deploy

1. Redeploy previous Portal artifact  
2. Do **not** reverse migrations unless reviewed (see Upgrade Validation)  
3. Set `RA_MOCK_GUACAMOLE=true` only in non-prod emergency  
4. Stop Guacamole stack if desktop is the only failure mode; sync path can continue  

Related: [RemoteAnalysisGuacamoleDeployment.md](../../RemoteAnalysisGuacamoleDeployment.md)
