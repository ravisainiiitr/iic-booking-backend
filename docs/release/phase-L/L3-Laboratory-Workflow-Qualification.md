# Phase L3 — Laboratory Workflow Qualification Report

**Date:** 2026-08-06  
**Portal:** https://equip.iitr.ac.in  
**Equipment:** Powder X-Ray Diffractometer (PXRD) [A] (`equipment_id=1`)  
**Backend at end of L3:** `v2.5.0-rc24-release` (`b3bf95c`)

## Summary

| Step | Internal (faculty) | External | Notes |
|------|--------------------|----------|-------|
| Booking | PASS `316` / `IICPXRD [A]202600023` | PASS `319` / `IICPXRD [A]202600026` (also `317`,`318` pre-fix) | Valid XRD inputs A=1,B=10,C=80,D=0.02,E=0.5 |
| Approval / create | PASS (wallet debit at book) | PASS (₹944 incl. GST) | Status pending→booked path OK |
| Sample submission | PASS `SAMPLE_SENT` | PASS `SAMPLE_SENT` | |
| Hold / Forward | N/A | PASS `HELD_AT_OFFICE` → `FORWARDED_TO_LAB` | Required for external |
| Sample acceptance | PASS | PASS (after rc24) | See production fix |
| Result upload / sync | PASS | PASS | DSA Active path → S3 |
| Result download | PASS (faculty zip) | PASS (operator); **gated for user** | External user blocked until I-STEM FBR verified (by design) |
| Booking completion | PASS | PASS | |
| Invoice PDF | PASS (2475+ bytes) | PASS (2507 bytes) | |
| Wallet deduction | PASS (faculty ~₹40) | PASS (₹944 at create) | Balance observed declining across external books |
| Notifications | PASS | PASS | `Booking - Created` etc. |

## Production fix (L3)

- **Phase:** L3 Laboratory Workflow  
- **Workflow:** External sample acceptance after Hold/Forward  
- **Exact failure:** `POST .../sample-trace/set/` with `SAMPLE_ACCEPTED` returned HTTP 400  
  `"Only Hold Booking (Held at Office) or Forward to Laboratory is allowed for external bookings at this time."`  
  even after successful `HELD_AT_OFFICE` and `FORWARDED_TO_LAB`.  
- **Root cause:** Operator branch in `set_booking_sample_status` restricted **all** non–Sample-Sent statuses on external bookings to Hold/Forward only, so lab could never accept.  
- **Minimum corrective action:** Allow operators to set `SAMPLE_ACCEPTED` / `SAMPLE_REJECTED` / `PROCESSING` after Hold or Forward (`b3bf95c`, tag `v2.5.0-rc24-release`, Deploy run `31074355922` PASS).  
- **Re-test:** Booking `319` trace = SENT → HOLD → FWD → **SAMPLE_ACCEPTED**; results synced; complete + invoice OK.

## By-design residual

- External **user** result list/download requires verified I-STEM FBR (`istem_fbr_not_executed`). Operators can download; presigned S3 object URLs work.

## Verdict

**L3 PASS** on PXRD for ≥1 internal and ≥1 external user after rc24.
