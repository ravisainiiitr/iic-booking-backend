# Commissioning Run Observability

Engineering support for Phase 2 live commissioning. Does **not** change the
commissioning workflow or user permissions.

## Purpose

Every toolkit / live commissioning execution can produce:

1. A unique **CommissioningRunId**
2. An **execution timeline** with per-step timing and success/failure
3. An **evidence ZIP** (admin/manage only)
4. A **failure snapshot** when a step fails

Use these artifacts to determine root cause without reproducing the failure.

## Storage locations

| Artifact | Location |
|----------|----------|
| Run / timeline / snapshots | DB tables `CommissioningRun`, `CommissioningRunStep`, `CommissioningFailureSnapshot` |
| Evidence ZIP | Django storage: `remote_analysis/commissioning_runs/<run_id>/evidence.zip` |
| Portal audits / events | Existing `WorkspaceAudit` / `WorkstationEvent` rows; details / `correlation_id` tagged with Run ID when a run is in context |
| Agent file logs | Not pulled automatically — on Analysis PC under `C:\ProgramData\RemoteAnalysisAgent\Logs\` (placeholder note inside the ZIP) |

Django admin: **Commissioning run** (and related step / failure snapshot models).

## How Run ID is attached

- Context var `ra_commissioning_run_id` (request/thread scoped)
- Prefixed into audit/event details: `[commissioning_run=<uuid>]`
- Set as `WorkstationEvent.correlation_id` when available (including heartbeat-related events for workstations with an active run)
- Structured logs include `commissioning_run_id`
- Optional body field on commissioning actions: `commissioning_run_id`
- Connectivity / self-test create a run automatically (or accept an existing id)

Lifecycle phase changes on a workspace that is linked to a **RUNNING** run map into timeline steps (agent download, verification, analysis, collect, checksum, cleanup) without altering sync behavior.

## Admin APIs (CanManageRemoteAnalysis)

Base: `/api/v1/analysis/operations/toolkit/`

| Method | Path | Purpose |
|--------|------|---------|
| `GET` / `POST` | `runs/` | List recent runs / start a run |
| `GET` | `runs/<uuid>/` | Summary + timeline |
| `GET` | `runs/<uuid>/timeline/` | Timeline JSON |
| `GET` / `POST` | `runs/<uuid>/evidence/` | Download evidence ZIP (`POST` or `?rebuild=1` regenerates) |
| `GET` | `runs/<uuid>/failures/` | Failure snapshots |

Connectivity and self-test responses include:

```json
{
  "commissioning_run_id": "<uuid>",
  "evidence_url": "/api/v1/analysis/operations/toolkit/runs/<uuid>/evidence/"
}
```

Toolkit HTML shows the Run ID and an evidence download link after connectivity / self-test.

## Evidence ZIP contents

- `commissioning_summary.json`
- `execution_timeline.json`
- `portal_logs.json` (audits / events filtered by Run ID / linked workspace)
- `agent_logs.json` (pointer only)
- `workspace_metadata.json`
- `api_summary.json` (includes failure snapshot payloads)
- `checksum_results.json`
- `performance_metrics.json`
- `commissioning_report.pdf` (or `commissioning_report_error.txt` if PDF render fails)

## Recommended live-run procedure

1. `POST …/toolkit/runs/` with `workstation_id` → note `commissioning_run_id`
2. Pass that id into toolkit connectivity / self-test and into commissioning console actions as `commissioning_run_id`
3. On completion or failure, download `…/runs/<id>/evidence/`
4. Inspect `execution_timeline.json` and any `failures/` snapshots first

## Related docs

- [RemoteAnalysisLiveCommissioning.md](RemoteAnalysisLiveCommissioning.md)
- [RemoteAnalysisCommissioningToolkit.md](RemoteAnalysisCommissioningToolkit.md)
- [sat/12-Live-Commissioning-Report.md](sat/12-Live-Commissioning-Report.md)
