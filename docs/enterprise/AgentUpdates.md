# Agent Updates

## DSA

Reuse Milestone 16: `/api/v1/sync/updates/discover|report|status|history/` and `ReleasePackage` / `UpdateDeployment` staged rollout.

## RAA

Discover + report client (Phase 2.5): `EnableAutoUpdateDiscover` polls installer latest every 6 hours and posts status.

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/v1/analysis/installer/releases/latest/` | Enrollment key, agent bearer, or RA manage |
| GET | `/api/v1/analysis/updates/discover/` | Same (alias) |
| POST | `/api/v1/analysis/updates/report/` | Same |

No auto-install in Phase 2 / 2.5.

## Deployment Center

Installer catalog + wizard compatibility JSON, repair/emergency package fields, SHA-256, signature status, rollback_of pointer.
