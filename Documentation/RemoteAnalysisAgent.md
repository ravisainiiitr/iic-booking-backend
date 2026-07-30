# Remote Analysis Agent — Deployment (Phase 2 / Workstream 1)

Companion Windows service for `iic_booking.remote_analysis`.

Repository: `D:\IIC_NEW\RemoteAnalysisAgent` (separate from DSA).

## Portal APIs consumed (unchanged)

- `POST /api/v1/analysis/register/`
- `POST /api/v1/analysis/heartbeat/`
- `POST /api/v1/analysis/inventory/`
- `GET /api/v1/analysis/commands/`
- `POST /api/v1/analysis/commands/{id}/complete/`

## Install (Analysis PC)

1. Install .NET 10 Windows Runtime.
2. Publish / copy build to `C:\Services\RemoteAnalysisAgent`.
3. Set `RemoteAnalysisAgent:PortalBaseUrl` and `EnrollmentKey` (match portal `RA_AGENT_ENROLLMENT_KEY`) in `appsettings.json`.
4. Run `scripts\install-service.ps1 -PortalBaseUrl https://equip.iitr.ac.in` as Administrator.
5. Confirm workstation appears on portal `/remote-analysis` after ~30s.
6. Optional local probe: `GET http://127.0.0.1:5088/api/health` (`LocalHealthPort`; set `0` to disable).

State: `C:\ProgramData\RemoteAnalysisAgent\State\agent-state.json`  
Logs: `C:\ProgramData\RemoteAnalysisAgent\Logs\`

## Offline / reconnect

Transient portal HTTP failures use exponential backoff (`MaxRetryAttempts` / `RetryBaseDelaySeconds`). After auth loss the agent re-registers. Heartbeats resume automatically when the portal is reachable again.

## Automatic workspace sync

On `PREPARE_WORKSTATION` / `SYNC_WORKSPACE` the agent creates `Input|Working|Output|Logs|Temp`, downloads portal RawData into `Input` using the **manifest** (sha256 verified; skips unchanged), and on `COLLECT_WORKSPACE` uploads `Output`/`Logs` (skips files already on portal). Cleanup respects `defer_output_cleanup` until portal reaches **UploadVerified**. See Portal doc `AutomaticDataSynchronization.md`.

## Relation to DSA

DSA (`DepartmentSyncAgent`) handles instrument booking sync/uploads.  
RAA (`RemoteAnalysisAgent`) handles analysis PC pool health and session prepare/cleanup.  
Do not merge the two agents.
