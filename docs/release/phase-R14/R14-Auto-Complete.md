# R14 — Equipment Auto-Complete

Per-equipment setting that automatically marks a booking **COMPLETED** after the scheduled end time **only when meaningful result data exists**.

## Setting

- Field: `Equipment.auto_complete_booking` (boolean, default `False`)
- Independent of `enable_remote_analysis`
- Configured on Equipment create/edit (portal Equipment form)
- Not a global flag

## Rule

Auto-complete runs only when **all** of the following are true:

1. `equipment.auto_complete_booking = True`
2. Booking scheduled end time has passed
3. Booking is in a completable state (`PENDING`, `BOOKED`, `PROCESSING`)
4. A `BookingWorkspace` (Active folder record) exists
5. `has_material_result_files()` finds real result data (S3 + DSA + operator uploads)
6. There is **no** active Remote Analysis session (`OPEN_SESSION_STATUSES`)

Empty `Result/` folders, `workspace-ready` markers, hidden/system files, and temp extensions (`.tmp`, `.partial`, …) do **not** count.

## Implementation (reuse)

- Periodic task: `equipment.auto_complete_bookings_with_data_after_end` (existing Celery beat, 15 min)
- Domain service: `booking_completion_service.try_auto_complete_booking`
- Result detection: `booking_results_service.has_material_result_files`
- Email: existing `_send_completion_email_with_attachments(booking, [])` — **no file attachments**
- Audit: `BookingEvent` with `completion_source=AUTO_COMPLETE`

## Idempotency and races

Row `select_for_update` on the booking. A second run, or a concurrent manual Complete, yields exactly one COMPLETED transition and one completion event.

## Active Remote Analysis

If a live RAA session exists, auto-complete **skips** the booking. The session is not terminated.
