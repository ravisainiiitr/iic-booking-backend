# Phase 8B — Legacy Booking Blocking

## Design (hybrid)

Do **not** copy old bookings into `equipment.Booking`.

| Layer | Role |
|-------|------|
| `LegacyBookingBlock` | Audit / reconciliation / abort metadata |
| `DailySlot.status=BLOCKED` + `blocked_label=LEGACY_MIGRATION:{legacy_id}` | Occupancy enforced by existing claim path |

Canonical create path continues to use:

`DailySlot.objects.select_for_update(... status=AVAILABLE)`

Blocked migration slots therefore cannot be claimed. When the unavailable slots are migration-labelled, the API returns:

- HTTP **409**
- `code`: `LEGACY_MIGRATION_SLOT_BLOCKED`
- User message: slot temporarily unavailable due to previous portal booking during migration

## Window

Configured on `PortalMigrationState`:

- `migration_start_at`
- `migration_window_end_at`

Window semantics for discovery: `migration_start_at <= booking.start < migration_window_end_at`

Timezone: application `TIME_ZONE` (Asia/Kolkata) with `USE_TZ=True`. Never use operator machine TZ.

## Discovery

```bash
python manage.py migration_discover_legacy_bookings --fixture-file rows.json
```

Live MySQL booking column map is **not** hard-coded. Supply normalized fixture rows until a verified column map is approved.

## Arm / release / abort

- `arm_legacy_block(...)` — creates ACTIVE block + marks AVAILABLE overlapping slots BLOCKED
- `release_legacy_block(...)` — restores claimed slots to AVAILABLE when still labelled
- `abort_migration_batch(...)` — releases ACTIVE blocks for a batch; preserves audit rows; does **not** reverse Phase-8A refunds

## Reconciliation

```bash
python manage.py migration_reconcile_legacy_blocks
```

Detects missing slot links, unexpected slot state, blocks outside window.

## Freeze mode

`PortalMigrationState.booking_migration_mode`:

`NORMAL | PREPARATION | FREEZE | ACTIVE | SETTLEMENT | COMPLETED`

At T0 (operator-controlled; **not** auto-activated by this phase):

| Surface | Behavior |
|---------|----------|
| OLD portal (external) | New booking / reschedule / waitlist / sample → blocked (`MIGRATION_BOOKING_DISABLED`) |
| NEW portal (this app) | Booking remains enabled when `end_user_booking_enabled=True`; respects legacy blocks |
| Legacy blocks | ACTIVE |

This Django app **is** the new portal. Old-portal API freeze must be enforced on the external old portal (or a shared bridge consuming `/api/portal-migration/booking-status/`).

## Phase-8A interaction

Migration refund does **not** free slots and does **not** release `LegacyBookingBlock`.
