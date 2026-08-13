# R12 — Architecture (Human-Friendly Select Analysis Data)

## Scope

Workstream A delivers a **human-friendly Select Analysis Data** experience in Analysis Workspace:

- Browse **Current Data** and **Previous Data** for authorized bookings
- Search/filter by sample, equipment, booking reference, file/folder names, date, file type
- Preview metadata before staging
- Select a file or folder; portal stages into workspace `RawData`
- Block RAA allocation when workstation `disk_low` is true
- **No** DSA↔RAA coupling inventiveness; DSA and RAA remain independent

Out of scope for this workstream: PI pricing / EquipmentPI / ChargeProfilePricingProfile.PI.

## Reuse map

| Concern | Existing module |
| --- | --- |
| List RAW/results for a booking | `BookingRawStagingService.list_raw_entries` |
| Merge S3 + DSA + operator uploads | `equipment/booking_results_service.py` |
| S3 Results keys / bytes | `sync/services/results_s3.py` + `api_views._list_booking_result_files_from_s3` |
| Stage into workspace | `BookingRawStagingService.stage_into_workspace(..., entries=)` |
| Workspace facade | `BookingWorkspaceFacade` |
| Auth helpers | `_can_access_analysis_files`, `_can_launch` in `remote_analysis_integration/views.py` |
| Allocation gate | `AvailabilityEngine.evaluate` |
| FE shell | `AnalysisWorkspace.tsx`, `api.ts` |

## New pieces

- `AnalysisDataBrowserService` — metadata-only dataset assembly + selection persistence
- `GET .../analysis/data-browser/` — authorized browse API
- `POST .../analysis/data-selection/` — record selection + optional stage
- FE `SelectAnalysisDataBrowser` dialog
- `AvailabilityEngine` reason **Disk space low** when `workstation.disk_low`

## Security

- File browser requires owner or elevated analysis staff (same as analysis files APIs)
- Faculty same-department summary access does **not** grant data-browser enumeration
- Responses never include permanent public S3 URLs or presigned URLs
- Path traversal (`..`) rejected on selection

## DSA coexistence

DSA continues to publish into `Results/{virtual_booking_id}/` and `ResultAttachment`. RAA consumes portal workspace sync only. R12 does not scan lab PC disks and does not invent DSA→RAA direct coupling.

## Allocation / disk_low

RAA already reports `diskLow` / `disk_low` on heartbeat. Portal now refuses allocation when the flag is set. No RAA code change required for this gate.
