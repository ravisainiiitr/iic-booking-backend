# Headless Department Sync Agents: remote observability and troubleshooting

Department Sync Agents (DSAs) are meant to run on department PCs with no monitor and nobody at the PC. The Main
Administrator has to be able to see whether every DSA is working, find out why one isn't, and fix the common
problems from the portal. This document lists what that needs, what existed before this change, and what the change adds.

## Gap analysis

| What the Main Administrator needs | Before | Now |
|---|---|---|
| Service up, version, uptime | Version and uptime were on each heartbeat row. "Online" only on the Agents tab. | `diagnostics.service`: version, process start, uptime, whether it runs as a Windows service, memory. The fleet view shows health per DSA. |
| Last heartbeat, portal reachability | `last_heartbeat_at` only. A DSA that stops reporting was shown as offline, with no alert. | Offline is evaluated every 5 minutes on the portal, opens a `DSA_OFFLINE` incident and alerts after a grace period. The DSA reports its last portal success and how many heartbeats failed in a row. |
| Enrollment / token state | Not reported. | `diagnostics.enrollment`: enrolled, token valid, expiry, last token error (redacted). `TOKEN_INVALID` (critical) and `TOKEN_EXPIRING` (warning). |
| Disk space, SQLite DB size | Disk % per heartbeat, no DB size, no alert. | Free space on the data drive plus SQLite and WAL sizes. `DISK_LOW` at 20% free (warning) and 10% (critical); `SQLITE_LARGE` at 2 GB. |
| Equipment Network adapter and IP | Not reported (visible only on the PC). | Adapter found, connected, IP, configured IP. `EQUIPMENT_NETWORK_DOWN` (critical) and `EQUIPMENT_NETWORK_IP` (mismatch). |
| Each Equipment PC: online, last seen, agent version, share OK | Rollup was stored inside heartbeat JSON but shown nowhere. | Shown in a per-DSA table. `EQPC_OFFLINE` (not seen for 10 min) and `EQPC_SHARE` incidents per PC. |
| Workspace counts | Portal-side list only. | DSA-side counts by state. |
| File detection and upload queue: pending, failed, last error, oldest stuck item | Queue size only. | Waiting, uploading, retrying and failed counts, oldest waiting item (age, file, retries), last error (redacted), last completed upload, last file detected, files queued in 24 h. `UPLOADS_STUCK` (oldest > 60 min, critical) and `UPLOADS_FAILED`. |
| Live-feed sessions | Not reported. | Number of feeds running. A restart is refused while a feed runs unless forced. |
| Recent warnings and errors | Only through the RUN_DIAGNOSTICS / COLLECT_LOGS commands. COLLECT_LOGS is unredacted. | Error and warning counts for the last 1 h and 24 h, plus the latest 15 lines, all redacted. `ERROR_SPIKE` at 20 or more errors in an hour. |
| UPS / power | UPS readings were already on the UPS health page. | Number of UPS units polled and readings queued. |
| Time skew | Not measured. | The DSA sends `sent_at`; the portal stores the skew. `CLOCK_SKEW` above 120 s. |
| Incident timeline | Generic `AlertEvent`s from the M15 telemetry path, which the DSA does not use. | Headless incidents (`AlertEvent`, `details.source = "headless"`) are opened and resolved automatically. They form each DSA's timeline, which is kept for 180 days. |
| Alerts | None for DSAs. | Email and bell to Main Administrators, only after a problem has lasted 10 minutes. A reminder follows every 12 hours. Acknowledging an incident pauses reminders. A bell message is sent when the DSA recovers. |
| Remote actions | Generic command queue, open to staff. RESTART_AGENT did nothing. | The fleet view sends audited actions, Main Administrator only: restart service, re-sync bookings, retry failed uploads, collect support bundle, refresh Equipment PCs, run diagnostics. Each action is checked against what the agent version supports. A second request for the same action is refused while the first is waiting, and restarts have a 10-minute cooldown. |

## Heartbeat contract (backwards compatible)

DSA 1.0.7 and later add the following to `POST /api/v1/sync/heartbeat/`:

- `sent_at`: the agent's clock, in ISO 8601.
- `details.diagnostics`: an object with `diagnostics_version: 1` and these sections: `service`, `portal`,
  `enrollment`, `storage`, `equipment_network`, `uploads`, `detection`, `workspaces`, `live_feed`, `ups`, `problems`.
  A section that fails to collect is sent as `{"error": "..."}`.
- `details.capabilities.remote_commands` lists the commands the agent runs, and
  `details.capabilities.headless_diagnostics` gives the diagnostics version.

Older portals ignore the new fields, but they store `details.diagnostics` on every heartbeat row. **Deploy the backend
before installing DSA 1.0.7.** Older agents keep working: the fleet marks them `NO_DIAGNOSTICS`, and the dashboard
disables actions they can't run.

## Storage and retention

- `AgentDiagnosticState` holds one row per agent, overwritten on each heartbeat, so diagnostics are never stored per
  heartbeat.
- `AgentHeartbeat` rows are deleted after `DSA_HEARTBEAT_RETENTION_DAYS` (30), up to 5,000 per run.
- `AgentSupportBundle` zips are capped at 1.5 MB on the agent and 3 MB on the portal, and deleted after 30 days.
- Resolved headless incidents are deleted after 180 days.

## Settings (all optional)

`DSA_ALERT_NOTIFY` (True), `DSA_ALERT_GRACE_MINUTES` (10), `DSA_ALERT_REPEAT_HOURS` (12), `DSA_EQPC_OFFLINE_MINUTES` (10),
`DSA_UPLOAD_STUCK_MINUTES` (60), `DSA_DISK_WARN_PERCENT` (20), `DSA_DISK_CRITICAL_PERCENT` (10), `DSA_SQLITE_WARN_BYTES`
(2 GB), `DSA_ERROR_SPIKE_PER_HOUR` (20), `DSA_CLOCK_SKEW_SECONDS` (120), `DSA_TOKEN_EXPIRY_WARN_HOURS` (72),
`DSA_HEARTBEAT_RETENTION_DAYS` (30), `DSA_SUPPORT_BUNDLE_RETENTION_DAYS` (30), `DSA_INCIDENT_RETENTION_DAYS` (180),
`DSA_SUPPORT_BUNDLE_MAX_BYTES` (3 MB).

## How the DSA runs each action

| Action | Command | What the agent does |
|---|---|---|
| Restart service | `RESTART_AGENT` | Refused when the agent isn't running as a Windows service, within 10 minutes of the last restart, or while a live feed runs (unless forced). Otherwise it reports the command complete, then a detached PowerShell runs `Restart-Service DepartmentSyncAgent` 5 s later. Uploads that were in flight resume after the restart. |
| Re-sync bookings | `SYNCHRONIZE_BOOKINGS` | Refreshes the configuration and processes portal bookings once. |
| Retry failed uploads | `RETRY_FAILED_UPLOADS` | Moves failed upload-queue rows back to Retrying with the retry count reset (up to 500), resets failed detected files and rescans. Nothing is deleted. |
| Collect support bundle | `COLLECT_SUPPORT_BUNDLE` | Builds an in-memory zip: diagnostics, redacted logs, heartbeat history, open uploads, Equipment PCs and non-secret configuration. The portal stores it apart from the command, and it can be downloaded from the fleet view. |
| Refresh Equipment PCs | `REFRESH_EQUIPMENT_PCS` | Runs an SMB write probe on each registered Equipment PC, with a 30 s timeout per PC. |

The agent redacts text before it leaves the PC: bearer and basic tokens, `password=`/`token=`-style values, JWTs,
e-mail addresses, Indian mobile numbers, Windows/Unix user profile names, and long hex or base64 strings.

## Console traceability (14 Nov 2026)

The DSA Monitor (`/department-sync?tab=fleet`) drills down fleet -> agent -> Equipment PC -> booking. The fleet API
adds, without a migration:

- `GET /api/v1/sync/admin/fleet/?include_retired=1` returns retired/revoked agents too (`retired: true`); by default
  they stay hidden and `retired_count` says how many. Retired agents never count towards health totals and accept no
  remote actions (the action endpoint still 404s for them); their detail page stays readable.
- Per agent: `latest_version` (the installer release marked latest), `version_status`
  (`current`/`outdated`/`ahead`/`unknown`), `share` (results shares ok/failing/unknown across Equipment PCs),
  `network` (Equipment Network adapter summary), `errors_1h`/`errors_24h`/`warnings_24h`, `last_command`, and
  `equipment_pc_hosts` (host names and IPs, for search only).
- Detail: `sync_events` (warning/error SyncLog rows of the last 14 days with event code, equipment and booking id),
  `uploads` (last 30 days of upload sessions with booking and result-processing outcome), `workspaces` (booking
  workspaces), and `equipment_pcs[].last_probe` (the latest Refresh Equipment PCs probe for that PC).

Firewall state is not reported by DSA 1.0.10 itself; the console judges it from the SMB write probe that
"Refresh Equipment PCs" runs (connectivity, SMB status, write test, error category).
