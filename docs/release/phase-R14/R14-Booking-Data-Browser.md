# R14 — Booking Data Browser

Reuses R12 dataset metadata (S3 / DSA / operator files via `BookingRawStagingService.list_raw_entries`). No second filesystem scanner.

## Current Booking Data

- Scope: this booking only
- Filters **removed**: Equipment, Sample, File type
- Search: virtual booking ID, sample identifiers, file name, folder name — still constrained to the current user’s bookings for this equipment
- Heading: **virtual booking ID** (never a numeric index such as `"1"`)
- Expand is lazy (`source_booking_id` fetch)

## Previous Booking Data

- Same owner + same equipment, excluding the current booking
- Same simplified chrome (no equipment/sample/file-type filters)
- Pagination (`page_size` default 20)

## Confirmation

Continue → summary (booking, data, files, size) → **Use This Data →** persists selection and returns to the workspace to start the existing RAA flow.
