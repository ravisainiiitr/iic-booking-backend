# Disaster Recovery

Remote Analysis Platform — RC1.

## What to back up

| Asset | Method | Retention (recommended) |
|-------|--------|-------------------------|
| PostgreSQL (Portal DB) | Logical dump / managed snapshot | 30 daily + 12 monthly |
| Workspace files (`workspace_root`) | Volume snapshot / rsync | Match `retention_days` (≥90) |
| Archive files (`archive_root`) | Volume snapshot | ≥ retention policy |
| Reports media | With MEDIA or dedicated volume | 90 days |
| Agent local workspaces | Host backup optional (Portal is source of truth for exchange) | Per lab policy |
| Configuration | `RemoteAnalysisSettings`, env files, Traefik TLS secrets | Versioned secrets store |
| Celery beat DB schedules | Included in Portal DB | — |

## Restore procedures

### Database

1. Stop django/celery writers.
2. Restore PostgreSQL from last known-good dump/snapshot.
3. Start services; run `migrate --noinput` (should be no-op if backup includes applied migrations).
4. `GET /api/v1/analysis/health/ready/` → 200.
5. Spot-check reservations, sessions, workspaces.

### Workspace storage

1. Restore `workspace_root` / `archive_root` volumes to matching DB epoch when possible.
2. If DB newer than files, expect missing file audits — re-sync from Agent where available.
3. If files newer than DB, do not invent DB rows; restore DB first.

### Configuration

1. Restore env / secrets.
2. Confirm `RemoteAnalysisSettings` singleton values (especially Guacamole + `mock_guacamole=False`).

## Disaster recovery sequence

1. Declare incident; freeze deploys.
2. Restore DB → storage volumes → config.
3. Bring Redis/Celery healthy.
4. Start Portal; verify readiness probe.
5. Verify Agents reconnect (heartbeats).
6. Clear orphan OPEN sessions older than policy via terminate/cleanup tasks.
7. Run smoke tests (TestingChecklist.md).
8. Communicate restoration to operators.

## Recovery verification checklist

- [ ] Health ready=200
- [ ] Agents online
- [ ] Sample reservation allocate
- [ ] Session launch (or documented Guacamole outage)
- [ ] Workspace list/upload
- [ ] Ops dashboard loads
- [ ] Notifications/activity readable
- [ ] Beat tasks present and enabled

## RPO / RTO targets (guidance)

- RPO: ≤ 24h (daily backups) or better with continuous snapshots
- RTO: ≤ 4h for Portal+DB; Agent hosts independent
