# R14 — Data Selection

Selection is recorded **before** RAA allocation so the user never waits for a PC merely to choose files.

## Persistence

`Booking.analysis_data_selection` (JSON) stores:

- `source`: `current` | `previous` | `upload`
- `source_booking_id` (internal PK)
- `virtual_booking_id` (display)
- `folder_path`, `file_names`, `file_count`, `total_size_bytes`
- `confirmed_at`

Refresh of `/analysis/` returns this payload so the choice survives queue waits.

## APIs

- `GET /api/v1/bookings/{id}/analysis/data-browser/`
- `POST /api/v1/bookings/{id}/analysis/data-selection/`
- Upload remains `POST .../analysis/files/upload/` (`auto_allocate=False`)

## Authorization (server-enforced)

- Caller must be booking owner (or analysis staff for browse)
- Source bookings must belong to the **same user** and **same equipment**
- Submitting another user’s booking id returns 403
- Virtual booking IDs are presentation-only; selection uses internal PK

## Staging

`analyze_data` reuses `BookingRawStagingService.stage_into_workspace` with optional `allow_names` / `folder_prefix` / `source_booking`. Upload selections are not re-copied from booking RAW.
