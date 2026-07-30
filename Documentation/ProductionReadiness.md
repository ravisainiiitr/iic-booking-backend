# Production Readiness

Milestone 8 — Enterprise production hardening for the Remote Analysis Platform.

**Status:** Release Candidate 1 (`1.0.0-rc1`)  
**Feature scope:** Complete through Milestones 1–7. Milestone 8 adds **no** user-facing features.

## Architecture summary

```
Equipment Booking Portal (orchestrator)
├── Workstation registry & Agent control plane (M1–M2)
├── Scheduler / reservations (M3)
├── Browser remote desktop / Guacamole (M4)
├── Analysis Workspace file exchange (M5)
├── Operations Center (M6)
├── Collaboration Center (M7)
└── Production hardening (M8): health, indexes, retries, docs, tests
         │
         └── Remote Analysis Agent (Windows service) — heartbeat, commands, workspace sync
```

Portal remains the sole orchestrator. Agent, Scheduler, Guacamole, Workspace, Operations, and Collaboration architectures are unchanged.

## Architecture validation

Run:

```bash
python manage.py validate_remote_analysis
```

Checks: package imports, migration chain 0001–0006+, URL names (including health probes), core service facades, RBAC presence, `/api/v1/analysis/` versioning.

Report artifact: `Documentation/ArchitectureValidation.md`.

## Performance

| Area | Hardening |
|------|-----------|
| Session expire/idle queries | Indexes `(status, expires_at)`, `(status, last_activity_at)` |
| Workspace purge | Index `(status, retention_until)` |
| Activity / assistance queues | Composite indexes on feed/verb/status |
| Invitations expiry | Indexes `(status, expires_at)` |
| Operations dashboard | `DashboardSnapshot` 60s cache (unchanged) |
| List APIs | `parse_pagination` on reservations / sessions / workspaces (`limit`/`offset`, max 200) |
| Workstation offline sweep | Index `(status, last_heartbeat)` (`0008_*`) |
| Celery | Autoretry + backoff + `acks_late` on RA periodic tasks |

## Database

Migrations `0007_production_hardening_indexes` and `0008_workstation_status_heartbeat_index` (additive indexes only — **no schema redesign**).

Guidance: nightly `VACUUM`/reindex on PostgreSQL where applicable; retain archived workspaces per `retention_days`; purge ops fine-grained metrics via `archive_old_metrics`.

## Security

See `Documentation/SecurityReviewChecklist.md`. Production must set `mock_guacamole=False`, HTTPS at Traefik, Guacamole credentials only in Portal settings (never to browsers).

## Reliability

Validated failure modes (docs + hooks): agent reconnect via heartbeat, Portal restart (stateless web + Redis Celery), Guacamole mock/unavailable path, workspace sync interruption + retry commands, reservation queue recovery, session cleanup after terminate/fail, invitation expiry idempotency.

## Monitoring

Operations Center KPIs + alert rules remain source of truth. Health probes:

- `GET /api/v1/analysis/health/live/`
- `GET /api/v1/analysis/health/ready/`
- `GET /api/v1/analysis/health/`

Agent: `GET http://127.0.0.1:{LocalHealthPort}/api/health` (default **5088**, loopback only; `0` disables).

## Logging

`RemoteAnalysisCorrelationMiddleware` assigns / echoes `X-Correlation-ID` on `/api/v1/analysis/*`. Celery beat jobs use `correlation_scope` + `structured_log`. Prefer including `session_id`, `reservation_id`, `workspace_id` in operational logs. Secrets via `mask_secret`.

## Configuration

Catalog: `remote_analysis/configuration_catalog.py`. Critical: `mock_guacamole`, `RA_*` Guacamole overlays, storage roots, quotas, Celery Redis URL, Agent `PortalBaseUrl` / `LocalHealthPort`.

## Testing

`iic_booking/remote_analysis/tests/` — health, permissions, path traversal, notifications, activity, validate command. Manual steps: `Documentation/TestingChecklist.md`.

## Deployment & DR

- `Documentation/DeploymentGuide.md`
- `Documentation/OperationsRunbook.md`
- `Documentation/DisasterRecovery.md`

## Known limitations

- Guacamole recording reserved / not implemented
- Virus scanner default `noop`
- SMS/WhatsApp/Push notification channels stubbed
- No real-time chat / collaborative editing (by design)
- OpenAPI annotations for RA endpoints are partial (platform schema via drf-spectacular)

## Future roadmap (non-blocking)

ClamAV integration, Guacamole recording, push notifications, zero-downtime blue/green refinements, expanded load tests.

## Release tag

Recommended Git tag: **`remote-analysis-v1.0.0-rc1`**
