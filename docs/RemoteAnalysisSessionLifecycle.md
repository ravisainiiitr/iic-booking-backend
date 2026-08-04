# Remote Analysis Session Lifecycle

This document captures the Remote Analysis session lifecycle, state transitions, workflow hooks, and failure handling for the B2 split scope.

## Scope boundary for B2

B2 includes lifecycle operations and orchestration:
- Session creation and launch progression
- PREPARE and COLLECT workflow continuation
- Extend Analysis
- End Analysis
- Upload Past Data
- Session cleanup, timeout, workstation release, reservation completion

B2 excludes:
- Equipment configuration and migrations (B3)
- Software-aware allocation and queue/waiting policy logic (B4)
- Check-in-specific two-stage reservation flow (`AWAITING_CHECKIN`) and check-in window policy (future/out-of-scope for B2)

## Session state machine

Primary session statuses (`SessionStatus`):
- `CREATED`
- `PREPARING`
- `READY`
- `TOKEN_GENERATED`
- `LAUNCHED`
- `CONNECTING`
- `CONNECTED`
- `ACTIVE`
- `IDLE`
- `DISCONNECTING`
- terminal: `COMPLETED`, `EXPIRED`, `FAILED`, `TERMINATED`

### Implemented transitions in lifecycle code

From `iic_booking/remote_analysis/guacamole/session.py` and `iic_booking/remote_analysis/guacamole/cleanup.py`:

1) Create / prepare path
- `"" -> CREATED` on session creation.
- `CREATED -> PREPARING` when `PREPARE_WORKSTATION` is issued.

2) Prepare success path
- `PREPARING -> READY` after input is prepared and validated.
- `READY -> TOKEN_GENERATED` when ephemeral Guacamole connection exists and/or launch token is issued.

3) Browser launch / connect path
- `TOKEN_GENERATED|READY -> LAUNCHED` when launch URL is issued.
- `LAUNCHED|READY|TOKEN_GENERATED -> CONNECTING` when token is consumed.
- `CONNECTING -> CONNECTED -> ACTIVE` when browser connects.
- `IDLE -> ACTIVE` when activity resumes.

4) Idle/timeout/termination path
- `ACTIVE|CONNECTED -> IDLE` when near idle timeout threshold.
- Any open session -> `DISCONNECTING` when explicit terminate is requested.
- Cleanup terminal outcomes:
  - -> `TERMINATED` (manual/user end path)
  - -> `EXPIRED` (session expiry timeout)
  - -> `FAILED` (prepare/guacamole failures or explicit fail path)

### Reservation/workstation transitions coupled to session lifecycle

Reservation transitions:
- `RESERVED -> PREPARING` when session creation starts.
- `PREPARING -> READY` when workstation preparation succeeds.
- `READY|PREPARING|RESERVED -> ACTIVE` when browser connects.
- `ACTIVE|READY|PREPARING|RESERVED -> COMPLETED` during cleanup/end-analysis release path.

Workstation transitions:
- `* -> PREPARING` at session prepare issue.
- `* -> BUSY` when browser session becomes active.
- `* -> AVAILABLE` during cleanup (unless `DISABLED` or `MAINTENANCE`).

## PREPARE workflow (B2-relevant behavior)

- Session creation issues `PREPARE_WORKSTATION`.
- Command completion handling in `iic_booking/remote_analysis/services/commands.py` uses `transaction.on_commit(...)` to:
  - mark workspace prepared/failure safely after DB commit;
  - retry session advancement (`retry_prepare`) without rollback coupling.
- Timeout handling:
  - `_wait_prepare(...)` fails session with `"Preparation timeout"`.
  - periodic task `advance_preparing_sessions` also fails stale PREPARING sessions on timeout.

## COLLECT workflow and upload orchestration

- Cleanup issues `COLLECT_WORKSPACE` at session end.
- Workspace sync phases progress through:
  - `COLLECTING_OUTPUT -> UPLOADING_OUTPUT -> UPLOAD_VERIFIED -> CLEANUP -> COMPLETED` on success.
- Output cleanup is deferred until upload verification is complete.
- Verified cleanup issues `CLEAN_WORKSTATION` for Output/Logs deletion only after UploadVerified.

## Extend Analysis

- `extend_analysis(...)` extends `expires_at` for an active session by configured/fallback minutes.
- In current mixed implementation, queue fairness blocking (`WAITING` queue check) exists but is designated B4 scope and should not be included in B2 staging.

## End Analysis

- `end_analysis(...)` terminates active session if present, otherwise releases live reservation.
- It then releases workstation state and marks reservation completed through cleanup/release flow.
- In current mixed implementation, queue drain (`process_queue`) is present but belongs to B4 scope and should be excluded from B2 staging.

## Upload Past Data

- `upload_past_data(...)` uploads user file into workspace (`RawData` by default) and attempts sync command issue.
- Upload response includes file metadata and sync command id when available.

## Failure handling validation

### End Analysis failure
- Implemented.
- `SessionError` raised for authorization/no-active-session conditions.
- Unexpected errors are caught and normalized as `code="end_failed"` with server logging.

### S3 upload failure (workspace transfer/storage failure)
- Implemented.
- `TransferError` is converted to `SessionError(code="upload_failed" or transfer-specific code)`.
- Workspace sync tracks upload failures with `UPLOAD_FAILED` / `RETRY_PENDING` and supports retry.

### Cleanup failure
- Implemented.
- Cleanup path wraps guacamole destroy, collect command issue, workstation release, reservation release, session save, stats save, termination record, and audit each in guarded try/except blocks.
- Failures are logged; cleanup continues best-effort and records partial completion.

### Timeout
- Implemented.
- Prepare timeout -> session `FAILED`.
- Session expiry timeout -> cleanup with terminal `EXPIRED`.
- Idle timeout -> cleanup with terminal `TERMINATED` after IDLE progression.

### Browser disconnect
- Partially implemented in B2 lifecycle.
- Explicit "browser disconnected" callback transition is not present in current B2 candidate changes.
- Practical handling is via idle/expiry cleanup and manual terminate paths.
- Explicit disconnect-event-driven transition is deferred.

### Guacamole disconnect
- Partially implemented in B2 lifecycle.
- Connection setup failure is handled (`guac_connect_failed` -> fail path).
- Destruction failure during cleanup is logged and terminal cleanup still proceeds.
- Explicit reactive transition on asynchronous guacamole-side disconnect event is deferred.

## Transitions deferred to future commits / out of B2

- Reservation check-in lifecycle:
  - `RESERVED -> AWAITING_CHECKIN -> RESERVED` style transitions.
  - `start_checked_in_session(...)` / `release_checkin(...)`.
  - Check-in expiry policy behavior.
- B4 queue/availability policy transitions:
  - Queue fairness gating in `extend_analysis(...)`.
  - Queue drain/reallocation side effect in `end_analysis(...)`.
  - Software-aware required capability transitions affecting reservation allocation.
- B3 equipment-driven session defaults:
  - Session timeout derivation from equipment (`analysis_default_session_minutes`) in `guacamole/session.py`.

