# R12 Qualification Report

**Date:** 2026-08-13  
**Release tag:** `v2.5.38-r12-pi-pricing`  
**Backend PRs:** [#78](https://github.com/ravisainiiitr/iic-booking-backend/pull/78) (MERGED)  
**Frontend PRs:** [#13](https://github.com/ravisainiiitr/iic-booking-frontend/pull/13) (MERGED)  
**Master tip (backend):** `4d222ca` (includes PI #79 merge)

## Verdicts

| Area | Status | Evidence |
|------|--------|----------|
| Overall R12 | **PARTIAL** | Code merged + unit tests PASS; live RAA E2E NOT TESTED |
| Human-Friendly Data Browser | **PARTIAL** | APIs + UI merged; Docker pytest PASS |
| Current / Previous Data | **PASS** (unit) | `test_data_browser_owner_sees_current_and_previous` |
| Search (sample / file) | **PASS** (unit) | `q=Si-wafer`, `q=prev.xy` |
| Authorization (stranger 403) | **PASS** (unit) | Owner OK / stranger denied |
| Faculty same-dept no files | **PASS** (unit) | Summary without file metadata |
| Selection without workspace | **PASS** (unit) | Records selection; staging deferred |
| S3 metadata browse (live) | **NOT TESTED** | No controlled live booking this cycle |
| RAA session lifecycle E2E | **NOT TESTED** | Not executed |
| Cleanup after session | **NOT TESTED** | Not executed |
| DSA ↔ RAA coexistence | **NOT TESTED** | Not executed |
| Failure isolation | **NOT TESTED** | Not executed |
| Frontend build | **PASS** | `npm run build` on master after #13+#14 |
| Production deployment | see consolidated report | Tag + Deploy Backend dispatched |

## Backend tests (Docker)

```
pytest …/test_r12_data_browser.py …/test_pi_pricing.py -q --nomigrations
→ 9 passed
```

Environment: `iic_booking_local_django` + Postgres `iic_booking_test` on host port 55432.  
`--nomigrations` used because full migrate hits **pre-existing** duplicate index `remote_anal_status_7b334f_idx` (existing failure, not introduced by R12/PI).

## Security review (code)

- Browse/select inject `_can_access_analysis_files` before listing or staging.
- Previous candidates scoped to **same user + same equipment + booking_id__lt**.
- Responses metadata-only (tests assert no `download_url` / `X-Amz-`).
- Sample label prefers `sample_trace_events.sample_identifiers`, then notes, then dynamic inputs.

## Remaining blockers

1. Live Analysis Workspace → Select Data → RAA → S3 → cleanup E2E  
2. DSA ↔ RAA concurrency on PXRD  
3. Production smoke of data-browser against an authorized live booking  
