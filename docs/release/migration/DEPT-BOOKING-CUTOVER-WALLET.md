# Department booking gates, cutover lock, faculty wallet sync — deploy notes

**Cutover:** 4 October 2026 00:00 Asia/Kolkata (`PortalMigrationState.booking_opens_at`).

## Migrations

```bash
python manage.py migrate users
```

Migration `0106_department_equipment_booking_and_cutover` adds:

- `Department.equipment_booking_enabled` (default `False` for all rows)
- Seeds `booking_opens_at = 2026-10-04 00:00:00+05:30` and `end_user_booking_enabled = False` on the solo `PortalMigrationState`

## IIC department (required for faculty wallet sync)

Faculty login sync credits the SubWallet for department name exactly:

**`Institute Instrumentation Centre`**

Confirm it exists (internal type preferred). If missing, create it in Admin before relying on login sync.

## Legacy MySQL

Set `OLD_MYSQL_HOST`, `OLD_MYSQL_USER`, `OLD_MYSQL_PASSWORD`, `OLD_MYSQL_DATABASE` (and optional `OLD_MYSQL_PORT`).
Or `LEGACY_MYSQL_STAGING_FIXTURE_MODE=True` for staging fixtures.
If MySQL is not configured, login still succeeds; wallet sync is skipped.

## After 4 October 2026

1. Login sync stops automatically (`faculty_wallet_sync_window_open()`).
2. Hard freeze ends when `now >= booking_opens_at`.
3. Main admin enables Equipment booking per department (`equipment_booking_enabled`).
4. Optionally set New booking enabled (`end_user_booking_enabled=True`) under Portal Migration admin UI.
5. Batch Celery legacy sync schedule is unchanged; login sync no longer runs after cutover.

## Verify

- Admin Departments: Equipment booking column/toggle (default Disabled).
- Portal Migration: booking opens-at + lock message editable.
- Faculty login before cutover: Wallet Legacy + New; IIC SubWallet matches legacy closing.
- Book equipment before opens-at: 403 for all roles with lock message.
