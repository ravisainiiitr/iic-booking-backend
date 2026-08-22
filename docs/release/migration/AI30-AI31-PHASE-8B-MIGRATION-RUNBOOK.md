# Phase 8B — Migration Runbook (staging)

**Production actions in this phase: NONE unless separately approved.**

## Prerequisites

1. Architecture audit accepted (hybrid DailySlot.BLOCKED).
2. Staging DB only for migrate/arm/cleanup tests.
3. Explicit equipment mappings entered (no fuzzy match).
4. Migration window set on `PortalMigrationState` (app TZ).
5. `new_portal_url` configured (not hard-coded in UI).
6. Phase-8A settlement paths verified on staging.

## Commands (staging)

```bash
# Apply users.0102 only on staging / local test DB — never production without gated MIGRATE
python manage.py migrate users 0102_legacy_equipment_booking_bridge

python manage.py validate_legacy_equipment_mapping
python manage.py migration_discover_legacy_bookings --fixture-file staging_legacy_bookings.json
python manage.py migration_dry_run --fixture-file staging_legacy_bookings.json
python manage.py migration_cleanup_test_accounts --dry-run
# only if dry-run clear and is_test_account-only:
python manage.py migration_cleanup_test_accounts --confirm-test-cleanup

python manage.py migration_reconcile_legacy_blocks
# abort before irreversible cutover:
python manage.py migration_abort_batch <id> --confirm-abort
```

## T0 checklist (operator; do not auto-run)

1. Dry-run verdict: `READY FOR MIGRATION`
2. External OLD portal: disable create/reschedule/waitlist/sample
3. Set `booking_migration_mode=ACTIVE` via admin state API
4. Keep NEW portal booking enabled
5. Arm eligible blocks from validated batch
6. Reconciliation OK
7. Phase-8A refunds remain manual for OIC / Main Admin

## Role matrix (summary)

| Action | Faculty/Student | Lab-in-Charge | OIC | Dept Admin | Main Admin |
|--------|-----------------|---------------|-----|------------|------------|
| Login / view legacy | Y | Y | Y | Y | Y |
| New booking (old portal) | N at freeze | N | N | N | N |
| New booking (new portal) | Y* | Y* | Y* | Y* | Y* |
| Migration refund | N | N | Y | N | Y |
| Mapping / migration control | N | N | N | N | Y |

\*Subject to normal RBAC + legacy slot blocks. End-user freeze (`end_user_booking_enabled`) is independent and still applies when set.

## Abort

Before COMPLETED / after financial settlement:

- Abort releases blocks and preserves audit.
- Does **not** silently reverse wallet credits; use approved financial workflow.

## Production safety confirmation

This implementation must not:

- SSH production
- Run production migrate
- Create production blocks
- Activate T0
- Clean production test accounts
- Issue production refunds
