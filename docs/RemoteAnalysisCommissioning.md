# Remote Analysis — First End-to-End Sync Commissioning (No Guacamole)

Prove Portal → Agent → Portal file synchronization with one booking, one workstation,
one workspace, one input file, and one output file. No Guacamole. No analysis software.

## Access

- HTML console (admin / manage permission):  
  `https://<portal>/api/v1/analysis/operations/commissioning/?view=html`
- JSON status (5s poll source):  
  `GET /api/v1/analysis/operations/commissioning/?workspace_id=<uuid>`
- Actions:  
  `POST /api/v1/analysis/operations/commissioning/action/`

### Required migration

Before first use, apply:

```bash
python manage.py migrate remote_analysis
```

Migration `0010_workspace_lifecycle_phases` adds `upload_verified_at` and expands `sync_phase` choices.
If this is missing, the console returns HTTP 500 with a migrate hint (and full traceback when `DEBUG=True`).

Also linked from Operations diagnostics usage.

---

## 1. Operator procedure

### Prerequisites

1. Agent service running on the Analysis PC (`RemoteAnalysisAgent`).
2. Agent **Available**, health **100**, heartbeats flowing.
3. One **COMPLETED** booking on equipment with `enable_remote_analysis=True`.
4. Admin account with Remote Analysis manage permission.
5. Sample files ready:
   - `sample-input.txt` (any small text)
   - `sample-output.txt` (dummy result)

### Steps

| # | Action | Where | Success signal |
|---|--------|-------|----------------|
| 1 | Open commissioning console | Browser `...?view=html` | Page loads, workstation listed |
| 2 | Select completed booking + workstation | Setup panel | Dropdowns populated |
| 3 | **Create Workspace** | Button | Workspace appears; event `WorkspaceCreated` |
| 4 | Choose `sample-input.txt` → **Upload Input** | Setup panel | Input file listed with SHA-256 under RawData/ |
| 5 | **Prepare Workspace** | Button | Command `PREPARE_WORKSTATION` queued → DELIVERED → COMPLETED |
| 6 | Wait ≤30s | Auto-refresh | Phase **InputReady**; agent log `DownloadComplete` + `WaitingForAnalysis` |
| 7 | **Pause** | Analysis PC | Copy `sample-output.txt` into `C:\ProgramData\RemoteAnalysisAgent\Sessions\<reservation_id>\Output\` |
| 8 | **Collect Output** | Button | `COLLECT_WORKSPACE` completes; Output file appears under Processed/; phase **UploadVerified** then **Completed** (CLEAN may auto-queue) |
| 9 | **Cleanup Workspace** (if CLEAN not already done) | Button | Session folder removed; workstation **AVAILABLE**; agent log `Idle` |
| 10 | **View Logs** | Button | Portal lifecycle events + transfer retries visible |

Acceptance: one input on portal → same file on agent Input/ → one output on agent → same file on portal Processed/ → clean disk → Available.

---

## 2. Expected API sequence

```http
# Status
GET /api/v1/analysis/operations/commissioning/
Authorization: Session / JWT (admin)

# Create
POST /api/v1/analysis/operations/commissioning/action/
{"action":"create","booking_id":12345,"workstation_id":"<uuid>","ingest":false}

# Upload sample input
POST /api/v1/analysis/operations/commissioning/action/
Content-Type: multipart/form-data
action=upload&workspace_id=<uuid>&folder=RawData&file=@sample-input.txt

# Prepare (issues PREPARE_WORKSTATION)
POST /api/v1/analysis/operations/commissioning/action/
{"action":"prepare","workspace_id":"<uuid>"}

# Agent (automatic)
GET  /api/v1/analysis/commands/
GET  /api/v1/analysis/workspaces/<id>/manifest/
GET  /api/v1/analysis/workspaces/<id>/files/<file_id>/content/
POST /api/v1/analysis/commands/<cmd_id>/complete/
{"success":true,"message":"Prepared session ...; Downloaded 1 file(s)..."}

# Collect
POST /api/v1/analysis/operations/commissioning/action/
{"action":"collect","workspace_id":"<uuid>"}

# Agent (automatic)
GET  /api/v1/analysis/commands/
POST /api/v1/analysis/workspaces/<id>/agent-upload/   # multipart Output→Processed
POST /api/v1/analysis/commands/<cmd_id>/complete/

# Cleanup (manual or auto after UploadVerified)
POST /api/v1/analysis/operations/commissioning/action/
{"action":"cleanup","workspace_id":"<uuid>"}
```

---

## 3. Expected database changes

| Step | Tables / fields |
|------|-----------------|
| Create | `analysisreservation`, `analysisworkspace` (status CREATING/READY), booking FKs linked, folders created |
| Upload | `workspacefile` RawData/… with `sha256`, `source=portal` |
| Prepare queued | `remotecommand` PREPARE_WORKSTATION PENDING; `sync_phase=DownloadingInput`; workstation PREPARING |
| Prepare done | command COMPLETED; `sync_phase=InputReady`; `status=READY`; `last_synced_at` set; `workspaceaudit` Commissioning:InputReady |
| Collect queued | COLLECT_WORKSPACE; `status=COLLECTING`; phase UploadingOutput |
| Collect done | `workspacefile` Processed/… `source=agent`; `upload_verified_at`; phase UploadVerified → Completed; CLEAN queued |
| Cleanup done | CLEAN COMPLETED; agent state AVAILABLE; session folder gone |

Useful checks:

```sql
SELECT status, sync_phase, sync_progress_percent, sync_message, upload_verified_at
FROM remote_analysis_analysisworkspace WHERE id = '<uuid>';

SELECT command_type, status, result_message FROM remote_analysis_remotecommand
ORDER BY created_at DESC LIMIT 10;

SELECT relative_path, sha256, source FROM remote_analysis_workspacefile
WHERE workspace_id = '<uuid>' AND deleted = false;
```

---

## 4. Expected directory structure (Windows agent)

After Prepare / DownloadComplete:

```
C:\ProgramData\RemoteAnalysisAgent\Sessions\<reservation_id>\
  Input\sample-input.txt
  Working\
  Output\                 ← operator drops sample-output.txt here
  Logs\
  Temp\
  datasets\               (legacy mirror of Input)
  work\                   (legacy)
```

After Cleanup:

```
C:\ProgramData\RemoteAnalysisAgent\Sessions\<reservation_id>\   ← removed
```

State file retains AgentId/token:

```
C:\ProgramData\RemoteAnalysisAgent\State\agent-state.json
```

---

## 5. Expected logs

### Portal (`WorkspaceAudit.details` / console events)

- `Commissioning:WorkspaceCreated`
- `Commissioning:CommandQueued | PREPARE_WORKSTATION …`
- `Commissioning:InputDownloading`
- `Commissioning:InputReady`
- `Commissioning:WaitingForAnalysis`
- `Commissioning:CollectRequested`
- `Commissioning:UploadVerified`
- `Commissioning:Completed`
- `Commissioning:CleanupStarted` / `CleanupFinished`

### Agent (`C:\ProgramData\RemoteAnalysisAgent\Logs\raa-*.log`)

- `Polling`
- `CommandReceived | Type=PREPARE_WORKSTATION`
- `WorkspaceCreated | SessionId=…`
- `Downloading`
- `DownloadComplete | …`
- `WaitingForAnalysis`
- `CommandReceived | Type=COLLECT_WORKSPACE`
- `Uploading`
- `UploadVerified | Uploaded=1 …`
- `Cleanup`
- `Idle | Workstation AVAILABLE`

---

## 6. Recovery if a step fails

| Failure | Recovery |
|---------|----------|
| Create fails (eligibility) | Confirm booking COMPLETED + equipment RA enabled; or use a known eligible booking |
| Upload fails | Check extension allow-list / virus scanner settings; try `.txt` |
| Prepare stuck PENDING | Confirm agent online + heartbeats; check enrollment/token; restart agent service |
| Prepare FAILED / checksum | Re-upload input; click Prepare again; inspect agent log for mismatch |
| InputReady but no local files | Verify `session_id` = reservation id; check Sessions folder name |
| Collect finds no Output | Ensure file is under `Output\` not Input; retry Collect |
| Collect FAILED | `POST /workspaces/{id}/retry-transfer/` or Collect again; keep Output until UploadVerified |
| Cleanup incomplete | Click Cleanup again; manually delete Sessions folder if needed; set workstation AVAILABLE in Admin |
| Phase not advancing after COMPLETED command | Check portal logs for `Failed to mark workspace synced` / `mark_prepared` exceptions |

Do **not** enable Guacamole until this checklist passes green once.
