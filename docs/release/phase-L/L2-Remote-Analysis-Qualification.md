# Phase L2 — Remote Analysis Qualification Report

**Date:** 2026-08-06  
**Portal:** https://equip.iitr.ac.in  
**Workstation:** `d5cb9ef0-db96-4d6c-aa5d-523087a3f9cc` (PHASE3-Commissioning-Laptop / RAVI)  
**Agent:** `raa-phase3-laptop-01` · service `RemoteAnalysisAgent` Automatic/Running · health `:5088`  
**Backend tag:** `v2.5.0-rc23-release` (`d006157`)  
**Booking under test:** `314` / `IICPXRD [A]202600021` (COMPLETED, RA-eligible)

## Summary

| Area | Result | Evidence |
|------|--------|----------|
| Agent registration | PASS | Live workstation enabled; archived duplicates disabled |
| Heartbeat | PASS | Fresh heartbeat; health_score 100 when healthy |
| Sticky BUSY deadlock | FIXED | Portal ignored agent AVAILABLE while BUSY; fixed in rc23 |
| Reservation lifecycle | PASS | create → AWAITING_CHECKIN → start → READY |
| Auto allocation | PASS | Allocated to RAVI |
| Queue | PASS | Queue entry observed; stale QUEUED cancelled via `/analysis/end/` |
| PREPARE_WORKSTATION | PASS | Completed; session files downloaded |
| Reverse tunnel JOIN/CLOSE | PASS | JOIN_TUNNEL / CLOSE_TUNNEL COMPLETED |
| Guacamole launcher | PASS | `/analysis/desktop/?view=html` HTTP 200 |
| Session end + cleanup | PASS | end → TERMINATED / reservation COMPLETED; agent+portal AVAILABLE |
| Second full cycle | PASS | Session `c0211922-…` PREPARING → TOKEN_GENERATED → end → AVAILABLE |
| Check-in release path | PASS | `/analysis/release/` then `/analysis/end/` clears hold; portal AVAILABLE |
| Command history | PASS | `/api/v1/analysis/commands/history/` returns COMPLETED CLEAN/PREPARE/JOIN |
| Concurrent sessions | N/A | Single live Analysis PC |
| Hard network cut of RA agent | DEFERRED | Cannot elevate to stop service / rewrite agent-state (Access Denied); reconnect verified via normal heartbeat resume after session cycles |
| Reservation expiry (time-wait) | PARTIAL | Check-in `expires_at` returned (~10 min window); full natural expiry soak not waited; release/end paths exercised |

## Production fix (L2)

- **Defect:** After session idle/CLEAN, portal kept `BUSY` forever because heartbeat refused to apply agent `AVAILABLE` while status ∈ {BUSY, CLEANING, PREPARING, RESERVED}.
- **Fix (`d006157` / `v2.5.0-rc23-release`):** Clear sticky statuses on idle heartbeat when no active hold; set AVAILABLE on successful `CLEAN_WORKSTATION` completion.
- **Deploy:** run `31072358753` PASS.

## Residual (non-blocking)

- Session UI status can remain `TOKEN_GENERATED` until browser Guacamole connect (token ready; not a transport failure).
- `/analysis/release/` may leave reservation `QUEUED` (`end_of_queue`); `/analysis/end/` completes it. Operators should prefer **End** for final cleanup.
- Orphan historical `ACTIVE`/`SessionActive` workspaces from prior commissioning still present (cleanup recommended).
- Concurrent multi-PC sessions not exercised (fleet size = 1 live).

## Verdict

**L2 PASS** for single-workstation production operational qualification on PXRD booking 314.
