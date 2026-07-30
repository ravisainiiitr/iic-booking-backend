# 07 — Recovery Procedures

Use during SAT-06 and production incidents. Prefer observe → contain → restore → verify (SAT-05 smoke).

## Agent crash

1. Confirm Windows service stopped / faulted.
2. Collect `Logs\raa-*.log` and `State\agent-state.json` (copy).
3. Restart service.
4. Expect heartbeat resume; status leaves OFFLINE.
5. If command stuck PENDING/RUNNING: complete/fail via ops or wait for expiry; re-issue prepare/collect if needed.
6. Verify no half-written Output marked verified on portal.

## Portal restart

1. Drain or note in-flight agent HTTP calls (expect retries).
2. Restart portal app / containers.
3. `GET /api/v1/analysis/health/ready/`
4. Agent heartbeats resume; poll commands again.
5. Open commissioning console; confirm workspace phase unchanged or correctly recovered.

## Redis restart

1. If Redis used for cache/channels only: restart; sessions may reset — re-login portal UI.
2. If unused in env: mark N/A.
3. Confirm Celery/RQ workers (if any) reconnect; no duplicate command execution storms.

## Network interruption

1. Disconnect agent NIC or block portal host briefly mid-download.
2. Expect FAILED or retry; health drops; OFFLINE if prolonged.
3. Restore network; heartbeat recovery; retry prepare/collect from console.
4. Confirm checksums after successful retry.

## Database restart

1. Stop DB briefly during idle (not mid-migration).
2. Portal readiness fails then recovers.
3. Spot-check workstation + workspace rows intact.
4. No partial migrations; `showmigrations remote_analysis`.

## Partial upload

1. Abort agent-upload mid-body.
2. Portal must **not** set `UploadVerified`.
3. Retry collect/upload; final SHA matches local Output file.

## Disk full

1. Fill volume hosting `ProgramData` (lab VM carefully).
2. Prepare/collect must fail loudly.
3. Free space; cleanup; AVAILABLE.

## Permission denied

1. Deny ACL on `Sessions\<id>`.
2. Command FAILED; audit error.
3. Restore ACL; retry.

## Corrupt workspace

1. Delete session folder while portal thinks InputReady.
2. Collect/prepare fails; operator recreates workspace or re-prepares per runbook.
3. Do not mark Completed.

## Lost agent token

1. Delete token from agent state or revoke on portal.
2. Re-register with enrollment key.
3. Confirm single workstation row; heartbeats OK.
