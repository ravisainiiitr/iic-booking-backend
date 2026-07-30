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

- Portal: application stdout / aggregated logging — `/api/v1/analysis/*` responses echo `X-Correlation-ID`
- Celery worker/beat logs (periodic jobs include correlation payloads via `structured_log`)
- Agent: `C:\ProgramData\RemoteAnalysisAgent\Logs\` (+ Windows Event Log when running as service)
- Guacamole: Guacamole server logs

## Troubleshooting quick hits

| Symptom | Check |
|---------|--------|
| Agent offline | Heartbeats, PC network, agent service status, `http://127.0.0.1:5088/api/health` |
| Reservation stuck QUEUED | Available workstations, maintenance windows, `process_reservation_queue` beat |
| Session fails to launch | `mock_guacamole`, Guacamole readiness check, RDP secrets on workstation |
| Workspace sync fail | Agent auth token, disk quota, command `WORKSPACE_SYNC`/`COLLECT_WORKSPACE` status, `sync_phase` on workspace detail |
| Collect failed / Output retained | `defer_output_cleanup` on CLEAN; use `POST .../retry-transfer/`; check Celery `retry_failed_workspace_collects` |
| Compose django unhealthy | `/api/v1/analysis/health/ready/` — DB / Redis / Guacamole / enrollment checks |
| Pre-deploy validation | `Documentation/DeploymentValidationReport.md`, `scripts/HealthCheck.ps1|.sh`, `VerifyPortal.ps1`, `VerifyAgent.ps1`, diagnostics `…/operations/diagnostics/?view=html` |
| Incident runbook | `Documentation/TroubleshootingGuide.md`, `PilotDeploymentChecklist.md` |

## Escalation

1. Lab operator → Department admin
2. Platform admin (Remote Analysis manage)
3. Infrastructure (DB/Redis/Guacamole)
