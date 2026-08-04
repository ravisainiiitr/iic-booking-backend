# Production Deployment Runbook

## Purpose

Controlled RC1 deployment of integrated platform without feature changes.

## Deployment Order

1. Pre-deployment checks and backups
2. Portal backend services (with migrations)
3. Frontend deployment
4. Deployment Center metadata validation
5. DSA installer/artifact publication
6. RAA installer/artifact publication
7. Wizard installer publication
8. Agent rollout waves (DSA/RAA)

## Container Restart Order

1. Database dependencies (Postgres, Redis)
2. Backend web/API
3. Celery worker(s)
4. Celery beat/scheduler
5. Gateway/Guacamole components
6. Frontend serving tier

## Migration Order

1. Take DB snapshot backup.
2. Run migrations once via backend control plane.
3. Confirm all target heads reached.
4. Run smoke checks for:
   - Remote Analysis
   - Sync/Enrollment
   - Deployment Center
   - Lab Infrastructure/SAT

## Installer Deployment Order

1. Publish DSA/Wizard metadata and packages through Deployment Center.
2. Publish RAA installer metadata and packages.
3. Validate ticketed downloads and checksums.
4. Execute pilot install on non-production nodes.

## Health Verification Checklist

- API health and readiness endpoints return healthy.
- Core queue/scheduler services processing normally.
- DSA enrollment + heartbeat successful.
- RAA registration + heartbeat + command poll successful.
- Session creation and launch flow smoke-tested.
- SAT dashboard loads and can query baseline data.
- Deployment Center serves expected release metadata.

## Rollback Procedure

1. Halt new writes where feasible.
2. Restore prior DB snapshot.
3. Redeploy previous stable backend/frontend images.
4. Revert Deployment Center active release pointers.
5. Pause new installer rollouts.
6. Validate core health, auth, and agent heartbeats.

## Disaster Recovery

- Maintain off-host DB backups and artifact snapshots.
- Preserve release manifest + ledger snapshots for audit reconstruction.
- Keep emergency credential rotation runbook available for agent auth compromise scenarios.

## Estimated Downtime

- Planned maintenance window: **30-90 minutes** (environment dependent).
- Potential extension if rollback or schema remediation is required.

## Approval Gates

1. Security sign-off
2. Operations sign-off
3. SAT/Readiness sign-off
4. Release manager GO/Conditional GO decision
