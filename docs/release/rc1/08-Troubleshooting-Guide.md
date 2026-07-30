# Remote Analysis — Troubleshooting Guide (RC1)

## Readiness not ready

| Check | Meaning | Fix |
|-------|---------|-----|
| `database` error | DB down | Restore DB connectivity |
| `cache` degraded | Redis/cache issue | Fix Redis or accept degraded cache |
| `guacamole=mock_forbidden_when_debug_false` | Mock still on in prod | `RA_MOCK_GUACAMOLE=false` + sync settings |
| `guacamole=misconfigured` | Missing URLs/creds | Set Guac env |
| `guacamole=unreachable` | Guac API down | Restart Guacamole; check API URL |
| `agent_enrollment=missing_…` | No enrollment key | Set `RA_AGENT_ENROLLMENT_KEY` |

## Agent offline

1. Service running?  
2. Clock skew / NTP?  
3. Portal URL / TLS / proxy?  
4. Token expired? Re-enroll  

## Workspace stuck

| Phase | Typical cause |
|-------|---------------|
| DownloadingInput | Agent/network/storage |
| VerifyingInput | Checksum mismatch |
| UploadingOutput | Agent upload / portal disk |
| CleanupFailed | CLEAN command failed |

Use commissioning console + toolkit logs. Prefer evidence ZIP with Run ID.

## Desktop launch codes

See Guacamole runbook table (`booking_ineligible`, `window_not_started`, `not_ready`, `guac_connect_failed`, …).

## File checksum mismatch

Portal SHA-256 is authoritative. Re-collect; do not mark success if verify fails.

## Celery not advancing sessions

Confirm beat + worker; task `remote_analysis.advance_preparing_sessions` must run ~1m.
