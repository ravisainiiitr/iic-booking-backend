# Phase L5 — Security Qualification Report

**Date:** 2026-08-06  
**Backend:** `v2.5.0-rc24-release`

| Check | Result | Evidence |
|-------|--------|----------|
| Unauthenticated bookings list | PASS | HTTP 401 |
| Bad API token | PASS | HTTP 401 on `/api/wallet/` |
| Student → admin users | PASS | HTTP 403 |
| Student → faculty results (IDOR) | PASS | HTTP 403 permission error |
| Faculty complete booking (privilege) | PASS | HTTP 403 operator-only |
| RA heartbeat without agent token | PASS | HTTP 401 |
| Results POST misuse | PASS | HTTP 405 |
| External results without I-STEM FBR | PASS (by design) | HTTP 403 `istem_fbr_not_executed` |
| Login burst (25× bad password) | OBSERVED | All 401; no 429 in this burst — rate limit may be upstream/WAF or higher threshold |
| DSA enroll without secret | OBSERVED | Route 404 on probed paths (enrollment not anonymously exposed) |

## Residual

- Confirm production login rate limiting / WAF rules independently (burst did not return 429).  
- JWT/Token: DRF Token auth in use; rotate SAT tokens after go-live.  
- Replay protection for DSA uploads covered in prior DSA SAT; not re-fuzzed this phase.

## Verdict

**L5 PASS** for RBAC / auth / object-level access on exercised surfaces.
