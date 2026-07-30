# Troubleshooting Guide — Remote Analysis

**Audience:** Platform admins / lab IT  
**Related:** `OperationsRunbook.md`, `DeploymentValidationReport.md`, diagnostics at  
`GET /api/v1/analysis/operations/diagnostics/?view=html`

Quick probes:

```text
GET /api/v1/analysis/health/ready/
scripts/HealthCheck.ps1 -BaseUrl https://… -Token …
scripts/VerifyAgent.ps1
```

---

## Agent not registering

| Check | Action |
|-------|--------|
| Service running? | `Get-Service RemoteAnalysisAgent`; restart if stopped |
| `PortalBaseUrl` | Must be HTTPS Portal origin; no trailing path errors |
| `EnrollmentKey` | Must equal `RA_AGENT_ENROLLMENT_KEY`; 403 if mismatch |
| Portal reachable | `VerifyAgent.ps1` portal live check; firewall 443 |
| TLS / corporate proxy | Install CA; or investigate SSL errors in `ProgramData\…\Logs` |
| Duplicate agent_id | Clear state under `ProgramData\RemoteAnalysisAgent\State` only if intentional re-enroll |

---

## Heartbeat missing

| Check | Action |
|-------|--------|
| Heartbeat age on diagnostics | &gt;90s → offline |
| Agent logs | Auth expired → agent should re-register; check enrollment |
| Portal `/api/v1/analysis/heartbeat/` | Returns 401 without token — route must exist |
| Clock skew | Sync Windows time (NTP) |
| Rate limit / WAF | Ensure agent IPs allowed |

---

## Workspace preparation failure (`PreparationFailed`)

| Check | Action |
|-------|--------|
| Booking results seeded? | Booking must have DSA / result files; ingest logs in Portal |
| Agent prepare command | Commands history: PREPARE failed message |
| Disk / ACL | Session workspace root writable |
| Checksum mismatch | Corrupt download — retry prepare; check network |
| Quota | Portal storage / TransferPolicy limits |

---

## Synchronization failure

| Check | Action |
|-------|--------|
| `sync_phase` | Diagnostics workspaces by phase; RetryPending / UploadFailed |
| Manifest / agent upload | Agent logs for 403/409/400; path traversal rejected |
| `POST …/workspaces/{id}/retry-transfer/` | Re-issue COLLECT |
| Celery | `retry_failed_workspace_collects` enabled on beat |
| Mode 2 | `workspace_sync_mode=interval` only if intended |

---

## Guacamole unavailable

| Check | Action |
|-------|--------|
| Readiness `guacamole` | `ok` vs `unreachable` / `misconfigured` / `mock_forbidden_when_debug_false` |
| `RA_MOCK_GUACAMOLE` | Must be false in production |
| `RA_GUACAMOLE_*` | URL/creds; run `sync_remote_analysis_settings` |
| guacd / Guacamole containers | Compose health; admin login |
| TLS verify | `RA_GUACAMOLE_VERIFY_TLS` for internal CA |

---

## RDP connection failure

| Check | Action |
|-------|--------|
| InputReady gate | Session must not launch before InputReady |
| Guacamole → PC:3389 | Firewall / network path |
| RDP enabled on PC | System properties; NLA policies |
| Workstation credentials / Guacamole connection | Portal secrets store; do not expose to browser |
| Concurrent session caps | Settings `max_concurrent_sessions` |

---

## Cleanup failure (`CleanupFailed`)

| Check | Action |
|-------|--------|
| CLEAN_WORKSTATION command | Agent failed to delete folders — check process locks |
| Analysis software still open | Close tools; cleanup process names in agent options |
| Permissions | Service account ACL on Session root |
| Re-issue CLEAN | After UploadVerified, portal issues verified cleanup |

---

## Upload failure (`UploadFailed` / RetryPending)

| Check | Action |
|-------|--------|
| Output retained? | Expected — `defer_output_cleanup` until UploadVerified |
| Extension / size policy | TransferPolicy + settings blocked extensions / max size |
| Virus scanner noop | Infected path only if scanner enabled later |
| Network | Agent retries; then Portal retry-transfer |
| Nested paths | Agent sends `relative_path`; confirm Portal 201/409 |

---

## License unavailable

Remote Analysis does not ship a separate product license server. If users report “license unavailable”:

| Check | Action |
|-------|--------|
| Analysis application license on PC | MATLAB / Origin / vendor license servers must be reachable from the analysis workstation |
| VPN / firewall to license server | Lab IT |
| Concurrent license exhaustion | Vendor license admin |
| Wrong software inventory | Confirm agent inventory lists expected packages |

Portal “license” style errors on booking checkout are **booking billing**, not RA agent — check payments / charge profile separately.

---

## Escalation data to collect

1. `health/ready/` JSON  
2. Diagnostics JSON/HTML screenshot  
3. Workstation id / hostname / last heartbeat  
4. Session id + prepare/collect command ids  
5. Agent log tail (`ProgramData\RemoteAnalysisAgent\Logs`)  
6. Guacamole / guacd container logs  
