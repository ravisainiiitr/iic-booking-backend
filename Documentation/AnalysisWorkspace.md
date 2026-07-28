# Analysis Workspace & Secure File Exchange

Milestone 5 of the Remote Analysis Platform.

## Architecture

```
Equipment Booking Portal
  ├── Analysis Workspace (authoritative store)
  ├── Secure File Exchange (upload / download / zip)
  ├── StorageManager / TransferManager
  ├── Versioning + Quotas + VirusScan (IFileScanner)
  └── Remote Analysis Agent
          └── Secure local workspace folder
```

- Portal is the source of truth for experiment data.
- Guacamole remains display/keyboard/mouse only (Milestone 4 unchanged).
- Guacamole drive transfer is **not** the primary workflow.

## Workspace lifecycle

1. Reservation becomes active / session create  
2. Portal creates isolated `AnalysisWorkspace` + folder template  
3. Agent receives prepare/sync with `workspace_id`  
4. Agent pulls files into local secure directory  
5. User works via browser RDP  
6. On session end: agent collects outputs → Portal verifies SHA-256 → archive → cleanup local folder  

## Folder template

`RawData/`, `Processed/`, `Reports/`, `Exports/`, `Temp/`, `Logs/`, `Metadata/`  
Configurable via `RemoteAnalysisSettings.folder_template`.

## Synchronization flow

1. `WorkspaceSyncService.issue_sync_command` → agent `SYNC_WORKSPACE`  
2. Agent `GET .../workspaces/{id}/manifest/` (own workstation only)  
3. Incremental pull by SHA-256 comparison  
4. `COLLECT_WORKSPACE` uploads Processed/Reports/Exports/Logs  
5. Isolation: agent cannot access another reservation’s workspace  

## Transfer flow

- Portal upload: policy check → quota → write → SHA-256 → version → virus scan → audit  
- Download: integrity verify → FileResponse / ZIP  
- Chunk size / max sizes from settings  

## Versioning

New version on portal upload, agent upload, or replacement. History limited by `version_history_limit`.

## Security model

- Workspace isolation by reservation ownership + RBAC  
- Never expose absolute storage paths  
- Blocked/allowed extensions + per-department/workstation `TransferPolicy`  
- SHA-256 after every transfer  
- `IFileScanner` / `NoOpScanner` (Defender/ClamAV future)  
- Internal shares only (no anonymous public links)  

## Retention & archive

- `retention_days` sets `retention_until`  
- Archive = ZIP under `archive_root` with checksum  
- Restore verifies integrity then unpacks  
- Celery `purge_expired_workspaces` deletes past-retention archives  

## APIs

| Method | Path |
|--------|------|
| POST/GET | `/api/v1/analysis/workspaces/` |
| GET | `/api/v1/analysis/workspaces/{id}/` |
| POST | `.../upload/` |
| GET | `.../download/` |
| POST | `.../archive/` · `.../restore/` · `.../sync/` |
| GET | `.../files/` · dashboard |
| Agent | `.../manifest/` · `.../files/{id}/content/` · `.../agent-upload/` |

## Configuration (`RemoteAnalysisSettings`)

`workspace_root`, `archive_root`, quotas, retention, chunk size, compression flag, virus scanner, checksum algorithm, max upload/download, extension policies, folder template.

## Out of scope

Cloud/object storage, collaborative editing, real-time sync, cloud drives.

Migration: `0004_analysis_workspace_file_exchange`.
