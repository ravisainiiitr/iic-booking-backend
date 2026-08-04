# Database Qualification

## Scope

Portal migration sets from B1-B5 (with B6-B8 docs/stabilization scope), plus operational implications for DSA/RAA integration.

## Migration Ordering

Reference chain (validated in Phase 2.9):
- `remote_analysis`: `0017` -> `0018` -> `0019` -> `0020`
- `equipment`: `0182` -> `0183` -> `0184`
- `deployment`: `0001` -> `0002`
- `sync`: `0017` -> `0018`
- `lab_infrastructure`: `0001` -> `0002` -> `0003`

Result: ordering is monotonic with explicit cross-app dependencies and no detected loops.

## Dependency Integrity

| Check | Result |
|---|---|
| Missing dependencies | None detected |
| Duplicate migration numbers (audited apps) | None detected |
| Conflicting app heads in audited scope | None detected |
| Unreachable chains | None detected |
| Cross-app loop | None detected |

## Production Upgrade Path

1. Backup production database.
2. Validate current app heads and target heads.
3. Apply migrations in Django dependency-resolved order (`manage.py migrate`).
4. Validate post-migrate schema head alignment.
5. Run smoke checks for Remote Analysis, Sync, Deployment Center, Lab Infrastructure.

Risk: medium if applied without staged rehearsal and backup verification.

## Fresh Installation Path

1. Provision clean Postgres instance.
2. Apply full migration chain to latest heads.
3. Seed baseline admin/config data.
4. Register/validate DSA and RAA enrollment pathways.

Risk: low-medium; mostly operational config risk.

## Existing Installation Path

1. Confirm current production baseline and legacy migration heads.
2. Rehearse upgrade in staging with production-like dump.
3. Execute migrations with rollback-ready backup.
4. Reconcile data integrity for reservation/session/sync entities.

Risk: medium-high if rehearsal is skipped.

## Rollback Sequence

Recommended operational rollback:
1. Stop write-heavy services.
2. Restore full DB backup snapshot (preferred) instead of reverse-migrating complex cross-app chains.
3. Redeploy prior known-good app images/config.
4. Re-verify schema/application compatibility at rolled-back version.

Note: Logical reverse migration across all subsystems is not the preferred production strategy; snapshot restore is safer.

## Qualification Decision

- **Schema readiness**: Qualified structurally.
- **Operational readiness**: Conditional pending staged upgrade + rollback drill evidence.
