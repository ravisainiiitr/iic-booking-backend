# R14 — E2E Qualification Report

Status values used below are only **PASS / PARTIAL / BLOCKED / NOT TESTED**. PASS is used only when the check was actually executed.

| Area | Result | Evidence |
| --- | --- | --- |
| Backend unit tests (R7 completion + R14 auto-complete + data browser) | PASS | `pytest` 18 passed (`test_booking_completion_r7.py`, `test_r14_auto_complete.py`, `test_r14_data_browser.py`) |
| Backend RA integration smoke | PASS | `test_booking_remote_analysis_integration.py` 4 passed |
| Frontend unit tests (virtual ID helpers) | PASS | `npx vitest run src/components/analysis/dataBrowserUtils.test.ts` — 3 passed |
| Frontend production build | PASS | `npm run build` (vite 7.3.0) succeeded |
| Migration `0188_equipment_auto_complete_and_data_selection` | PARTIAL | Required on production `master` (0187 is already PI pricing). Not applied. |
| Controlled live E2E (portal + RAA PC) | PARTIAL | Owner `test.student@iic-booking.test`. Named booking `IICAPREO202600007` BOOKED → RA BLOCKED. Substitute `IICAPREO202600003`: Current/Previous/Upload PASS. Open Analysis Environment → QUEUED because `DESKTOP-CSMH6BU` offline (heartbeat ~19h stale). Guacamole ACTIVE not claimed. Details: `R14.3-Controlled-Real-Owner-E2E-Report.md` |
| Production deploy / smoke | NOT TESTED | Not executed; no production DB edits; 0188 not applied |
| Commit / push / merge / tag | NOT TESTED | Merge/tag/deploy gated on Guacamole ACTIVE + End Session/S3 |

Do not treat code existence as PASS.
