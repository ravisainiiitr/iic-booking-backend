# Automatic Data Synchronization — Remote Analysis

**Date:** 2026-07-30  
**Scope:** Portal `iic_booking.remote_analysis` workspace + Windows `RemoteAnalysisAgent`

## Goal

Users never manually copy experiment files over RDP. Booking/DSA results are seeded into the analysis workspace, downloaded to the PC before Guacamole launch, and uploaded back when the session ends — driven by **manifests** and **SHA-256 verification**.

## Architecture

```
Booking results (DSA ResultAttachment / BookingResultFile / S3)
        │
        ▼
BookingResultIngestService → WorkspaceFile under RawData/
        │
        ▼
Session create → PREPARE_WORKSTATION (manifest + workspace_id)
        │
        ▼
Agent WorkspaceTransferService
  • Ensure Input/Working/Output/Logs/Temp
  • Compare local vs portal manifest (sha256)
  • GET missing files; verify sha256 + size
        │
        ▼
Lifecycle → InputReady → Guacamole launch (gated)
        │
        ▼
User analyzes → saves under Output/
        │
        ▼
Session end → COLLECT_WORKSPACE (output manifest) → agent upload
        │
        ▼
UploadVerified → CLEAN (may delete Output) → Completed
```

Reuses existing workspace storage, TransferManager rules, agent command plane, and booking result APIs. **No parallel sync daemon.**

## Workspace lifecycle (`sync_phase`)

| Phase | Meaning |
|-------|---------|
| Preparing | Seeding booking results / creating workspace |
| DownloadingInput | Agent pulling Input |
| VerifyingInput | Checksums verified after download |
| InputReady | RDP may start |
| SessionStarting | Guacamole advancing |
| SessionActive | User connected |
| CollectingOutput | Session end collect started |
| UploadingOutput | Agent pushing Output/Logs |
| UploadVerified | Portal confirmed uploads |
| Cleanup | Agent deleting local folders |
| Completed | Done |
| PreparationFailed / UploadFailed / RetryPending / CleanupFailed / Cancelled | Failure / control |

Every transition is audited (`WorkspaceAudit` SYNC events) and survives restart via `AnalysisWorkspace.sync_phase`.

## Manifest format

```json
{
  "bookingId": "...",
  "sessionId": "...",
  "workspaceId": "...",
  "scope": "input",
  "files": [
    {
      "id": "...",
      "relativePath": "RawData/sample1.raw",
      "agent_relative_path": "Input/sample1.raw",
      "size": 25123456,
      "sha256": "...",
      "lastModifiedUtc": "...",
      "status": "Ready"
    }
  ]
}
```

- **Input scope:** RawData / Metadata  
- **Output scope:** Processed / Reports / Exports / Logs  
- Agent downloads only missing/mismatched hashes; uploads only modified files (409 Conflict = skipped).

## Folder mapping

| Agent PC | Portal workspace |
|----------|------------------|
| Input | RawData (+ Metadata) |
| Working | Temp |
| Output | Processed |
| Logs | Logs |
| Temp | Temp |

## Sync modes

| Mode | Setting | Behavior |
|------|---------|----------|
| Mode 1 (default) | `workspace_sync_mode=end_of_session` | COLLECT on session cleanup |
| Mode 2 | `workspace_sync_mode=interval` | Celery `interval_workspace_collect`; agent skips unchanged by checksum |

## Configuration (`RemoteAnalysisSettings`)

- `workspace_sync_mode`, `workspace_sync_interval_seconds`
- `transfer_max_retries`, `compression_enabled`, `compression_min_bytes`, `bandwidth_limit_kbps`
- Existing: quotas, allowed/blocked extensions, virus scanner hook, version history

## APIs

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/v1/analysis/workspaces/{id}/` | Includes `sync_*`, input/output files, transfers |
| POST | `/api/v1/analysis/workspaces/{id}/sync/` | Issue SYNC (input pull) |
| POST | `/api/v1/analysis/workspaces/{id}/retry-transfer/` | Retry failed collect/sync |
| POST | `/api/v1/analysis/workspaces/{id}/cancel-transfer/` | Manage: cancel in-flight |
| GET | `/api/v1/analysis/workspaces/{id}/manifest/` | Agent (input by default) |
| GET | `/api/v1/analysis/workspaces/{id}/files/{file_id}/content/` | Agent download |
| POST | `/api/v1/analysis/workspaces/{id}/agent-upload/` | Agent upload; **409** if unchanged sha256 |
| GET | `/api/v1/bookings/{id}/analysis/` | Booking summary includes `sync_phase` + `output_files` |

## Failure recovery

| Failure | Behavior |
|---------|----------|
| Checksum mismatch on prepare | PreparationFailed; no launch token |
| Network during upload | UploadFailed → RetryPending; Output retained |
| Unauthorized agent | 403 on manifest/content/upload |
| Session timeout | COLLECT then CLEAN with `defer_output_cleanup` until UploadVerified |
| Agent / portal restart | Resume via manifest checksum compare |

## Cleanup policy

- At session end: delete Input / Working / Temp; **retain Output** until `UploadVerified` (`upload_verified_at` set).
- After verified collect: portal issues CLEAN with `defer_output_cleanup=false` to remove Output/Logs.

## Administrator guide

1. Ensure booking has DSA/operator results before “Start Analysis”.
2. Configure sync mode and size/extension policies in Remote Analysis Settings.
3. Monitor ops transfer queue; use retry/cancel endpoints.
4. Do not force-delete Output on agent until collect succeeds.

## User guide

1. Complete experiment → results appear on booking.
2. Start Analysis → wait for **Input Ready**.
3. Work in `Input` / `Output` on the PC.
4. End session → download processed files from Portal / booking analysis.

## Migrations

- `remote_analysis.0009_auto_data_sync_fields` — sync_phase / settings (initial)
- `remote_analysis.0010_workspace_lifecycle_phases` — lifecycle enum + `upload_verified_at`
