# Phase L1 — Department Sync Agent Qualification Report

**Date:** 2026-08-06  
**Portal:** https://equip.iitr.ac.in  
**Agent:** IIC Agent (`4ce91b65-3ef5-42e6-8b89-faeb134c001b`) on host `RAVI`  
**Backend tag at end of L1:** `v2.5.0-rc22-release` (`ce04085`)

## Summary

| Step | Result | Evidence |
|------|--------|----------|
| 1 Investigate | PASS | Windows service `DepartmentSyncAgent` installed Automatic/Running; local health `:6001` Healthy; Production config + enrollment |
| 2 Heartbeat | PASS | Portal `online=true`, status `ENROLLED`, heartbeat age &lt; 60s |
| 3 Sync profile | PASS | Active assignment PXRD [A] profile `8f85c5d7-…`; watch path corrected to `D:\Results` |
| 4 Booking sync | PASS | Booking `314` / `IICPXRD [A]202600021` appeared in DSA `BookingCache` |
| 5 Sample accept | PASS | `SAMPLE_SENT` → `SAMPLE_ACCEPTED`; results folder resolved |
| 6 Result upload | PASS | Files detected under `D:\Results\Active\…`; portal `AgentUploadSession` COMPLETED; S3 download OK (operator) |
| 7 Completion sync | PASS | Portal complete → DSA `BookingCache` status `COMPLETED` |
| 8 Offline recovery | PASS | Portal URL disconnect → booking `315` not cached → restore → `SYNCED … BOOKED`; agent online again |
| 9 Concurrent agents | N/A | Only one live departmental agent (`SAT-L1-DSA` disabled) |
| 10 Large upload | PASS | 100 MB, 500 MB (interrupt+resume), 1 GB all COMPLETED on portal |

## Production fix applied during L1

- **Issue:** Invalid booking `input_values` raised formula `ValidationError`, then unguarded `transaction.set_rollback(True)` outside atomic → HTTP 500 `TransactionManagementError`.
- **Fix:** Guard `set_rollback` with `connection.in_atomic_block` (`ce04085`, tag `v2.5.0-rc22-release`, Deploy run `31070365222`).

## Configuration correction (ops, not code)

- Equipment sync profile UNC/watch was `\\192.168.1.2\Results` (unreachable). Updated to `D:\Results` and bumped `configuration_version`; agent re-bootstrapped.

## Residual notes

- DSA `/api/uploads` history can remain `Queued` after `UploadQueue` already `Completed` (dashboard inconsistency; transport OK).
- Service stop for offline test required elevation (Access Denied); connectivity loss simulated via local portal URL override.
- Concurrent multi-PC sync not exercised (single live agent).
