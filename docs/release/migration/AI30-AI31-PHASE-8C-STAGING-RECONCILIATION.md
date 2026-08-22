# Phase 8C — Staging Reconciliation

Generated from staging/test simulation (not production).

## Rules verified

| Rule | Expectation |
|------|-------------|
| Eligible legacy booking | Exactly one ACTIVE `LegacyBookingBlock` after T0 arm |
| Cancelled / completed / outside window / unmapped | No block from discovery (not armed) |
| Duplicate active block | Rejected (`duplicate_active_block`) |
| Abort | ACTIVE → RELEASED; audit row retained |
| Phase-8A settlement | Not silently reversed on abort |

## Command

```bash
python manage.py migration_reconcile_legacy_blocks
```

Checks active blocks vs `DailySlot.BLOCKED` labels and window bounds.

## Result (staging simulation)

See Phase 8C test `test_staging_t0_and_freeze_and_blocks` — reconciliation `ok=True` after arm; after abort, zero ACTIVE blocks for that batch.

**Production reconciliation:** not run.
