# RAA Handoff - Phase 2.9

## Enrollment

- RAA onboarding uses `/api/v1/analysis/register/` with agent identity and workstation metadata.
- Authentication lifecycle includes heartbeat and inventory posting before command execution.

## Reverse Tunnel

- Reverse tunnel schema restored in B1 via `remote_analysis/0017_restore_reverse_tunnel_transport.py`.
- Transport mode and tunnel gateway settings are carried by `RemoteAnalysisSettings` and tunnel session models.
- Command types `JOIN_TUNNEL` and `CLOSE_TUNNEL` are available in remote command orchestration.

## Session APIs

- Session lifecycle endpoints: `/api/v1/analysis/session/create/`, `/session/<id>/launch/`, `/connect/`, `/terminate/`, `/status/`, `/activity/`.
- Booking lifecycle endpoints: `/api/v1/bookings/<booking_id>/analysis/start|release|extend|end|files/upload/`.
- Reservation scheduling endpoints: `/api/v1/analysis/reservations/*`, `/availability/`, `/queue/`.

## End Analysis

- End-analysis backend path is owned by B2 and includes:
  - session closure transition
  - workspace/result collection
  - upload orchestration
  - cleanup and workstation release signals
- RAA must send final command/result state and telemetry so portal can close lifecycle consistently.

## Heartbeat

- Required endpoints: `/api/v1/analysis/heartbeat/` and associated command-poll/completion endpoints.
- Heartbeat freshness directly affects reservation/session readiness and scheduler release decisions.

## Required Portal behavior

- Maintain strict state transitions for reservation/session/tunnel orchestration.
- Allow additive compatibility with direct RDP and reverse-tunnel modes.
- Enforce auth and role boundaries between agent-side and portal-side endpoints.
- Preserve idempotent command completion semantics (duplicate complete/ack should not corrupt state).
- Keep timeout/cleanup paths deterministic to avoid orphan reservations, sessions, or workspaces.

