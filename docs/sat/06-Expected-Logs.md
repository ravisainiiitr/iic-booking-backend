# 06 — Expected Logs

## Portal

| Area | What to see |
|------|-------------|
| Registration | Accepted/rejected register; enrollment key failures |
| Heartbeat | Optional debug; offline detection warnings |
| Commands | Queue, deliver, complete, fail, expire |
| Commissioning | `Commissioning <event> \| workspace=…` info lines; exceptions on console failure |
| Auth | Unauthorized warnings for manage endpoints; HTML redirect (no JSON body for browser) |
| Transfers | Checksum mismatch, retry counts |

Audit tables: `WorkstationEvent`, `WorkspaceAudit`, command execution history.

Commissioning event markers (details contain):

`WorkspaceCreated`, `CommandQueued`, `InputDownloading`, `InputReady`, `CollectRequested`, `UploadVerified`, `Completed`, `CleanupStarted`, `CleanupFinished`, `WaitingForAnalysis`

## Agent (`C:\ProgramData\RemoteAnalysisAgent\Logs\raa-*.log`)

| Phase | Markers (representative) |
|-------|----------------------------|
| Start | Service start, AgentId loaded/created |
| Heartbeat | Periodic success / HTTP errors |
| PREPARE | Session folder create, DownloadComplete, WaitingForAnalysis |
| COLLECT | Upload of Output files, success/fail |
| CLEAN | Session remove, Idle |
| Crash recovery | Unhandled exception then restart; next heartbeat |

## SAT evidence rule

Attach **portal correlation**: workspace UUID + command UUID + approximate UTC timestamp matching agent log lines.
