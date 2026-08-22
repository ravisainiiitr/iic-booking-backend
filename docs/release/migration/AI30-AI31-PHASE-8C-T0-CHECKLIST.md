# Phase 8C — Staging T0 Checklist

**STAGING ONLY. Do not run on production.**

## Before T0

- [ ] Phase 8A tests PASS (22)
- [ ] Phase 8B tests PASS (21)
- [ ] `users.0101`–`0103` on staging/test DB only
- [ ] Explicit equipment mappings validated READY
- [ ] Window + `new_portal_url` configured (timezone-aware, app TZ)
- [ ] `migration_cleanup_test_accounts --dry-run` reviewed
- [ ] Fixture discovery classifications reviewed
- [ ] `migration_dry_run` → **READY FOR MIGRATION**
- [ ] Email dry-run counts reviewed; preview OK
- [ ] Mailpit (or staging SMTP) confirmed — **not** production SES

## T0 sequence (transactional)

1. Validate migration / dry-run READY  
2. Confirm test cleanup complete (if used)  
3. Confirm mappings complete  
4. Confirm migration window + URL  
5. Create `LegacyBookingMigrationBatch`  
6. Arm `LegacyBookingBlock` + `DailySlot.BLOCKED` **before** enabling freeze mode consumers  
7. Set `booking_migration_mode=ACTIVE` (old portal signal ON; new portal booking remains enabled)  
8. Create `MigrationNotificationBatch`  
9. Queue Celery email jobs (or `--email-dry-run`)  
10. Record `MigrationT0Event` timestamp  

```bash
python manage.py migration_staging_t0 \
  --fixture-file staging_eligible.json \
  --confirm-staging-t0 \
  --email-dry-run
# optional real Mailpit:
#   --queue-emails
```

## After T0

- [ ] Old portal action-gate → 403 `MIGRATION_BOOKING_DISABLED`
- [ ] New portal free slot bookable
- [ ] Overlap → 409 `LEGACY_MIGRATION_SLOT_BLOCKED`
- [ ] Reconciliation OK
- [ ] Mailpit subjects/templates verified (if queued)
- [ ] Abort tested on a disposable batch (optional)

## Abort

```bash
python manage.py migration_abort_batch <id> --confirm-abort
```

Releases ACTIVE blocks; keeps audit; does **not** reverse Phase-8A refunds.

## Production

Requires separate GO/NO-GO. Staging PASS ≠ production approval.
