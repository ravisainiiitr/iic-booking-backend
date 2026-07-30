# 04 — Expected Database Changes

Primary app: `remote_analysis` (+ booking FKs on equipment models).

## Registration

| Event | Tables / fields |
|-------|-----------------|
| First register | `AnalysisWorkstation` created (`agent_id`, `hostname`, status REGISTERING→ONLINE/AVAILABLE); `AgentToken` hashed; `WorkstationEvent` REGISTRATION |
| Re-register | Same PK/`agent_id`; token rotation per policy; inventory fields updated |
| Rejected enrollment | No new workstation (or no token issued); audit/auth failure |

## Heartbeat

| Event | Tables / fields |
|-------|-----------------|
| Normal | `WorkstationHeartbeat` row; `last_heartbeat`; `health_score`; possibly `TelemetrySnapshot` |
| Offline | status → `OFFLINE`; state history row |
| Recovery | status → `ONLINE`/`AVAILABLE`; heartbeat age resets |

## Workspace lifecycle

| Phase | Expected rows |
|-------|---------------|
| Create | `AnalysisWorkspace` (+ reservation if used); booking `analysis_workspace_id` / reservation link; folders metadata; audit `Commissioning:WorkspaceCreated` or SYNC |
| Upload input | `WorkspaceFile` under RawData/, `sha256`, size, source portal |
| Prepare queued | `RemoteCommand` PREPARE_WORKSTATION `PENDING`; `sync_phase=DownloadingInput` (or Preparing→DownloadingInput); workstation `PREPARING` |
| Prepare done | Command `COMPLETED`; `sync_phase=InputReady`; `last_synced_at`; audits InputReady / Waiting |
| Collect queued | COLLECT_WORKSPACE; status COLLECTING; phase CollectingOutput/UploadingOutput |
| Upload verified | Output `WorkspaceFile` Processed/; `upload_verified_at`; phase UploadVerified |
| Cleanup | CLEAN_WORKSTATION; phase Cleanup→Completed; workstation AVAILABLE; session files soft-deleted or marked cleaned |
| Delete | `WorkspaceStatus.DELETED` or archive path; no active FK cycles broken unexpectedly |

## Integrity checks (SAT-09)

```sql
-- Illustrative; adapt to actual table names in migrations
-- No workspace pointing at missing workstation when status ACTIVE
-- No COMPLETED command without completed_at
-- upload_verified_at IS NOT NULL implies phase in (UploadVerified, Cleanup, Completed)
```

## Orphans (must not remain after successful SAT-05)

- PENDING prepare/collect/clean for finished workspace without expiry
- `WorkspaceFile` rows for paths deleted on disk without soft-delete flag (lab check)
- Heartbeat flood without workstation FK
