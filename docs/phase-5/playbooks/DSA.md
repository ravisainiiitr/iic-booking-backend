# DSA Playbook

## Normal operation
- DSA enrolled, heartbeating, polling commands, and ACKing config.

## Monitoring
- Enrollment state, heartbeat freshness, command backlog, upload queue.

## Failure symptoms
- Missing heartbeat, command timeout, config ACK failure.

## Diagnosis
- Check local DSA logs, portal sync status, network reachability, token validity.

## Recovery
- Re-enroll if needed, restart agent service, replay pending commands safely.

## Escalation
- Lab ops -> DSA support -> Platform engineering.
