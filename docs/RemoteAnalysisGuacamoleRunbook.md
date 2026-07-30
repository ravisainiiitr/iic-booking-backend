# Guacamole Operational Runbook

## Daily checks

1. Toolkit → Guacamole tab: status `mock` (lab) or `ok` (prod), latency ms, active sessions  
2. Ready probe `checks.guacamole`  
3. Open sessions stuck in `PREPARING` / `CONNECTING`  

## User cannot see Launch button

Check HTML launcher JSON / page:

- Eligibility reason  
- Reservation + workstation assigned  
- Workspace exists  
- Guacamole `ok` or mock  

Path: `/api/v1/bookings/{id}/analysis/desktop/?view=html`

## Launch fails with code

| Code | Meaning | Action |
|------|---------|--------|
| `booking_ineligible` | Eligibility failed | Fix booking status / expiry / equipment flag |
| `window_not_started` | Too early for analysis window | Wait or adjust reservation / `analysis_available_from` |
| `reservation_inactive` / `expired` | Reservation closed | Recreate reservation |
| `no_workstation` | Not allocated | Allocate / process queue |
| `workstation_unhealthy` | Agent offline / bad status | Fix agent heartbeat |
| `capacity` | Global session cap | Wait or raise `max_concurrent_sessions` |
| `not_ready` | Prepare incomplete | Wait for agent prepare / check commands |
| `guac_creds` / `guac_connect_failed` | Guac provision/connect | Check Guac health, RDP secrets, base URL |

## Force disconnect

```http
POST /api/v1/analysis/session/{id}/terminate/
{"reason": "Operator forced disconnect"}
```

## Idle / max duration

Celery cleanup:

- Idle → `SessionCleanupService.cleanup_idle`  
- Max duration → expiry against `session.expires_at` / `session_timeout`  

## Evidence / audit

- Session audits: `GET /api/v1/analysis/session/{id}/audits/`  
- Workstation events: category SESSION / GUACAMOLE  
- Toolkit logs viewer  

## Guacamole down

1. Confirm `probe_guacamole` / ready probe  
2. Restart guacd + guacamole containers  
3. For open sessions: terminate in Portal (destroys ephemeral objects; re-launch creates new connection)  
4. If prolonged outage: enable mock only in non-prod; do not enable mock in production  

## Rollback

See [RemoteAnalysisGuacamoleDeployment.md](RemoteAnalysisGuacamoleDeployment.md#rollback).
