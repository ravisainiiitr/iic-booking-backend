# Remote Analysis Playbook

## Normal operation
- Reservation, check-in, launch, connect, end-analysis, and upload complete reliably.

## Monitoring
- Session state transitions, reservation queue, tunnel health, upload success rates.

## Failure symptoms
- Stuck session states, orphan reservations, launch/connect failures.

## Diagnosis
- Inspect session/audit logs, agent command status, gateway and storage connectivity.

## Recovery
- Trigger controlled terminate/cleanup, release reservation, retry launch or upload.

## Escalation
- Lab ops -> Remote analysis owner -> Backend platform team.
