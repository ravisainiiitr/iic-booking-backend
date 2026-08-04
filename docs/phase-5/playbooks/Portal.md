# Portal Playbook

## Normal operation
- Portal API, workers, scheduler, and core dependencies remain healthy.

## Monitoring
- Health/readiness endpoints, error rates, queue depth, DB latency.

## Failure symptoms
- 5xx spikes, auth failures, stalled queue, scheduler drift.

## Diagnosis
- Check service logs, DB/Redis connectivity, migration head, worker heartbeats.

## Recovery
- Restart affected service tier, restore connectivity, replay safe queued operations.

## Escalation
- Platform on-call -> Backend lead -> Release manager.
