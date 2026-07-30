# Remote Analysis Platform — Version 1.0 Release Notes (RC1)

**Version:** `1.0.0-rc1`  
**Recommended Git tag:** `remote-analysis-v1.0.0-rc1`  
**Date:** 2026-07-27

## Summary

The Remote Analysis Platform is **feature complete** through Milestones 1–7 and hardened for enterprise pilot deployment in Milestone 8. Portal remains the orchestrator; the Windows Agent, Scheduler, Guacamole session path, Analysis Workspace, Operations Center, and Collaboration Center are unchanged in architecture.

## Milestone overview

| M | Title | Outcome |
|---|-------|---------|
| 1–2 | Agent + Portal management | Registration, heartbeat, inventory, commands, dashboard |
| 3 | Scheduler | Reservations, allocation, queue, conflicts, maintenance |
| 4 | Browser remote desktop | Guacamole session lifecycle, one-time tokens |
| 5 | Analysis Workspace | Isolated storage, upload/download, sync, archive |
| 6 | Operations Center | KPIs, alerts, capacity, reports |
| 7 | Collaboration Center | Notifications, notes, sharing, assistance, timeline |
| 8 | Production hardening | Indexes, health probes, Celery retries, docs, tests, RC1 |

## Installation Guide (short)

1. Deploy Portal + Redis + Celery (+ Traefik).
2. Apply migrations; configure `RemoteAnalysisSettings`.
3. Install Agent on workstations; register to Portal.
4. Verify `/api/v1/analysis/health/ready/` and Agent `/api/health`.
5. Follow `Documentation/DeploymentGuide.md`.

## Administrator Guide (short)

- Manage workstations, maintenance, settings singleton, RBAC grants (`remote_analysis.*`).
- Production: set `mock_guacamole=False`, Guacamole URLs, storage roots, quotas.
- Monitor Operations Center; respond to alerts.
- Own backups and DR drills (`DisasterRecovery.md`).

## Operator Guide (short)

- View dashboards, heartbeats, queues, sessions, workspaces.
- Acknowledge alerts when permitted; assist users via Collaboration help queue.
- Do not change Guacamole secrets or production feature flags.

## Configuration Guide

See `iic_booking/remote_analysis/configuration_catalog.py` and ProductionReadiness.md. Critical flags: `mock_guacamole`, session/idle timeouts, workspace quota/retention, Celery Redis, Agent `PortalBaseUrl` / `EnrollmentKey` / heartbeat.

## API Summary

Base: `/api/v1/analysis/`

- Agent: `register`, `heartbeat`, `inventory`, `commands`
- Portal: workstations, dashboard, software, events
- Scheduler: reservations, availability, queue, candidates
- Sessions: create/launch/connect/terminate/status
- Workspaces: CRUD-ish, upload/download, sync, archive
- Operations: dashboard, analytics, utilization, alerts, reports
- Collaboration: activity, notifications, comments, notes, share, invite, assistance, timeline
- Hardening: `health/`, `health/live/`, `health/ready/`

## Migration Summary

`0001` … `0006` feature schema; `0007` production indexes (additive).

## Architecture Summary

Portal-orchestrated control plane; Agent executes; Guacamole for browser RDP; workspaces on Portal storage with Agent sync; ops + collaboration consume existing telemetry/events.

## Known Issues

- Default `mock_guacamole=True` unsafe if left on in production
- Virus scanner noop by default
- OpenAPI coverage incomplete for RA views
- Recording / push/SMS channels not implemented

## Future Enhancements

ClamAV, session recording, push notifications, expanded load testing, blue/green refinements.

## Commit message (suggested)

Finalize the Remote Analysis Platform for enterprise production by hardening architecture, validating reliability, optimizing performance, strengthening security, completing operational documentation, expanding testing, and preparing Release Candidate 1 without introducing new functional features.
