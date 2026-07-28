# Operations Runbook

Remote Analysis — day-2 operations (RC1).

## Daily

- Check Operations Center open alerts
- Confirm agent online count vs expected workstations
- Review Celery/Flower failed tasks
- Skim Collaboration assistance queue (pending help)

## Weekly

- Review utilization / capacity reports
- Confirm weekly report generation task succeeded
- Rotate / verify backup integrity (DB + workspace volumes)
- Review invitation expiry and stale assistance tickets

## Common procedures

### Agent offline

1. Confirm host power / network
2. Check Agent Windows service + `GET /api/health`
3. Verify Portal URL + token
4. Review Portal heartbeats / `AGENT_OFFLINE` alerts
5. Re-register only if token revoked

### Guacamole unavailable

1. Confirm Guacamole API health from Portal host
2. If emergency lab access needed, keep sessions from launching; do **not** flip `mock_guacamole` in production unless authorized
3. Raise / acknowledge Operations alert

### Stuck PREPARING session

1. Check prepare command completion on Agent
2. Celery `advance_preparing_sessions` should timeout/fail stale prepares
3. Terminate session from Portal if needed — cleanup command runs

### Workspace sync failure

1. Inspect last `WorkspaceTransfer` / audit
2. Re-issue sync from Portal Workspaces UI
3. Confirm disk quota and path isolation

### Reservation not allocating

1. Check queue + conflicts + maintenance windows
2. Confirm workstation health score ≥ allocation minimum
3. Run / wait for `process_reservation_queue`

## Maintenance windows

Create `MaintenanceWindow` rows; monitor task applies `MAINTENANCE` status. Announce via Collaboration announcements when user-impacting.

## Log locations

- Portal: application stdout / aggregated logging (include correlation IDs when instrumented)
- Celery worker/beat logs
- Agent: Windows Event Log / configured file sink
- Guacamole: Guacamole server logs

## Escalation

1. Lab operator → Department admin
2. Platform admin (Remote Analysis manage)
3. Infrastructure (DB/Redis/Guacamole)
