# R12 — Human-Friendly Data Browser

## User story

In Analysis Workspace, the researcher chooses analysis input by **sample / equipment / date / files**, not by memorizing booking IDs.

## API

### Browse

`GET /api/v1/bookings/{booking_id}/analysis/data-browser/`

Query params:

| Param | Meaning |
| --- | --- |
| `q` | Free-text search across sample, equipment, booking ref, folder, file |
| `equipment` | Equipment name/code filter |
| `sample` | Sample identifier filter |
| `date_from` / `date_to` | Booking slot/created date window |
| `file_type` | MIME or extension filter |
| `scope` | `current` \| `previous` \| `all` |
| `page` / `page_size` | Dataset pagination |
| `prefix` / `file_offset` / `file_limit` | Lazy folder file paging |
| `source_booking_id` | Restrict to one booking pk |

Response shape (abridged):

```json
{
  "datasets": [
    {
      "booking_pk": 123,
      "virtual_booking_id": "CHGEM202600001",
      "equipment_name": "PXRD",
      "sample_name": "Si-wafer-42",
      "booking_date": "2026-08-01",
      "booking_time": "10:30",
      "is_current": true,
      "folders": [
        {
          "name": "spectra",
          "path": "spectra",
          "files": [
            {"name": "run1.xy", "size_bytes": 1200, "type": "text/plain", "source": "booking_result"}
          ]
        }
      ]
    }
  ],
  "scope": "all",
  "pagination": {"page": 1, "page_size": 20, "total": 1, "has_more": false}
}
```

### Select

`POST /api/v1/bookings/{booking_id}/analysis/data-selection/`

```json
{
  "source_booking_id": 120,
  "folder_path": "spectra",
  "file_names": ["spectra/run1.xy"],
  "stage": true
}
```

Selection is stored on `AnalysisReservation.requested_resources.r12_data_selection` when a reservation exists; otherwise a compact marker is recorded on the workspace sync message. Staging uses `BookingRawStagingService.stage_into_workspace(..., entries=...)`.

## Frontend

- `SelectAnalysisDataBrowser` dialog from Analysis Workspace **Select Analysis Data**
- Tabs: All / Current / Previous
- Expand datasets → folders → files; preview metadata; select file or folder
- `apiClient.getBookingAnalysisDataBrowser` / `selectBookingAnalysisData`

## Tests

`iic_booking/equipment/remote_analysis_integration/tests/test_r12_data_browser.py`

- Owner browse current+previous + search
- Faculty same-dept denied for file browser
- Selection without workspace (deferred staging)
- AvailabilityEngine blocks `disk_low`
