# Architecture Validation Report

Generated for Milestone 8 / RC1. Re-run anytime: `python manage.py validate_remote_analysis`.

## Module boundaries

| Module | Responsibility | Depends on |
|--------|----------------|------------|
| `remote_analysis` core | Workstation registry, agent auth, commands | users RBAC |
| `services/` | Scheduler, allocation, audit | models |
| `guacamole/` | Session lifecycle, tokens | scheduler reservation, commands |
| `workspace/` | Storage, transfer, sync | session settings, reservations |
| `operations/` | KPIs, alerts, reports | telemetry from M1–M5 |
| `collaboration/` + packages | Notifications, sharing, assistance | sessions/workspaces |
| Agent (external) | Heartbeat, execute commands | Portal APIs only |

**Dependency direction:** Portal → Agent (commands). Agent never orchestrates Guacamole or Scheduler.

## API versioning

All Portal RA APIs under `/api/v1/analysis/`.

## RBAC

- `remote_analysis.view` / `remote_analysis.manage` seeded on migrate
- Permissions: `IsRemoteAnalysisAgent`, `CanViewRemoteAnalysis`, `CanManageRemoteAnalysis`
- Workspace share cannot bypass ownership/manage checks

## Migrations

| Migration | Milestone |
|-----------|-----------|
| 0001_initial_remote_analysis | M1–M2 |
| 0002_scheduler_reservation_engine | M3 |
| 0003_browser_remote_desktop_guacamole | M4 |
| 0004_analysis_workspace_file_exchange | M5 |
| 0005_operations_center | M6 |
| 0006_collaboration_center | M7 |
| 0007_production_hardening_indexes | M8 (indexes only) |

## Background jobs

19 Celery tasks registered via `signals.ensure_scheduler_periodic_tasks`, hardened with `ra_periodic_task` (autoretry, backoff, acks_late).

## Circular dependencies

Core facades import-clean: SchedulerService, SessionOrchestrator, WorkspaceSyncService, OperationsDashboardService, CollaborationDashboard.

## Dead code / unused surface (review notes)

- Guacamole recording flags reserved (documented unused)
- Notification SMS/WhatsApp/Push stubs intentionally unused
- No anonymous sharing models

## Configuration loading

`RemoteAnalysisSettings.get_solo()` + Agent `AgentOptions` + Django settings/Celery Redis.

## Findings (RC1)

| Severity | Finding | Disposition |
|----------|---------|-------------|
| Medium | OpenAPI annotations sparse for RA | Documented; schema available via platform spectacular where routed |
| Medium | Compose healthchecks not universal | Probes added; wire in Compose per DeploymentGuide |
| Low | Default `mock_guacamole=True` | Must disable in production checklist |
| Info | Feature complete M1–M7 | M8 hardening only |

**Verdict:** Architecture validated for Release Candidate 1.
