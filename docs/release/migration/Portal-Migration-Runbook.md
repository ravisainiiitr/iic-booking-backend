# Portal Migration Runbook (operator)

1. Enable Channel-I login on equip.iitr.ac.in (done).
2. Set `CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM=operator_confirmed_map` only after review.
3. Enable flags as needed: `DEPARTMENT_MAPPING_ENABLED`, `STUDENT_LIFECYCLE_ENABLED` (staging first).
4. Configure `OLD_MYSQL_*` read-only credentials for legacy sync.
5. Admin UI → Portal Migration: keep `end_user_booking_enabled=false` during parallel week.
6. Enable `incremental_sync_enabled`; watch watermark + exceptions.
7. Preflight + dry-run (no financial writes).
8. Final migration only with typed **MIGRATE** confirmation by Main Administrator.
9. After NEW_PORTAL_ACTIVE: freeze legacy ledger, enable booking, configure old portal redirect to https://equip.iitr.ac.in/

**Agents must not execute irreversible production cutover without explicit admin confirmation.**
