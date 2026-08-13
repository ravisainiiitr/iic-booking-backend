# R12 + Equipment PI — Consolidated Final Report

**Date:** 2026-08-13 (IST)  
**Release tag:** `v2.5.38-r12-pi-pricing`  
**Portal:** https://equip.iitr.ac.in  

## Merges

| PR | Repo | Result |
|----|------|--------|
| Backend #78 R12 | iic-booking-backend | MERGED `1c9221f` |
| Backend #79 PI | iic-booking-backend | MERGED `4d222ca` |
| Frontend #13 R12 | iic-booking-frontend | MERGED `e238c1b` |
| Frontend #14 PI | iic-booking-frontend | MERGED `863928e` (api.ts conflict resolved) |

## Git / release

| Item | Value |
|------|--------|
| Backend master | `4d222ca` |
| Frontend master | `863928e` |
| Tag | `v2.5.38-r12-pi-pricing` |
| Previous prod tag (rollback) | `v2.5.37-ra-fleet-noise-global` |
| RAA binary | **unchanged** — not redeployed |
| DSA | **unchanged** — not redeployed |

## Deployments

| Step | Status | Evidence |
|------|--------|----------|
| Frontend Deploy (master push #14) | **PASS** | Actions run `31665149277` success |
| Backend Deploy `v2.5.38-r12-pi-pricing` | **PASS** | Actions run `31665237881` success; EC2 `git describe` = tag |
| Migrate Production | **PASS** | Workflow `31665390532`; `0187` = `[X]` on prod django |
| Public `/api/version/` | **PASS** | HTTP 200 |
| Analysis health ready | **PASS** | HTTP 200, database/cache/gateway/guacamole ok |
| Frontend homepage | **PASS** | HTTP 200 |
| Capabilities | **PASS** | HTTP 200 |

## Itemized status (mandatory matrix)

| # | Item | Status |
|---|------|--------|
| 1 | R12 status | **PARTIAL** |
| 2 | Human-friendly data browser | **PARTIAL** |
| 3 | Current data | **PASS** (unit) |
| 4 | Previous data | **PASS** (unit) |
| 5 | Search/filter | **PASS** (unit) |
| 6 | Active folder | **PARTIAL** (code; live NOT TESTED) |
| 7 | File/folder selection | **PASS** (unit selection) |
| 8 | S3 | **NOT TESTED** (live) |
| 9 | RAA | **NOT TESTED** (this release; binary unchanged) |
| 10 | Cleanup | **NOT TESTED** |
| 11 | DSA/RAA concurrency | **NOT TESTED** |
| 12 | Failure-isolation | **NOT TESTED** |
| 13 | PI assignment | **PARTIAL** |
| 14 | PI charge profile | **PARTIAL** (code PASS; live NOT TESTED) |
| 15 | PI pricing | **PARTIAL** (unit PASS; live NOT TESTED) |
| 16 | Historical pricing | **PASS** (architecture) / live **NOT TESTED** |
| 17 | Authorization/security | **PASS** (code + unit) |
| 18 | Migration 0187 | **PASS** (production applied) |
| 19 | Backend tests (R12+PI) | **PASS** (9/9 Docker) |
| 20 | Frontend build | **PASS** |
| 21 | RAA tests | **NOT TESTED** (no RAA delta) |
| 22 | E2E results | **NOT TESTED** |
| 23 | Production deployment | **PASS** (BE+FE) |
| 24 | Production migration | **PASS** |
| 25 | Production smoke (read-only) | **PASS** |
| 26 | Git commits | see merges/tag above |
| 27 | PRs | #78 #79 #13 #14 merged |
| 28 | Release tag | `v2.5.38-r12-pi-pricing` |
| 29 | Rollback | Checkout previous tag `v2.5.37-ra-fleet-noise-global` via Deploy Backend; **0187 reverse migrate not verified** — prefer DB restore if schema rollback required |
| 30 | Remaining blockers | Live R12 E2E; DSA↔RAA concurrency; live PI quote/debit; authorized prod PI/R12 UI smoke |

## Final verdicts

| Stream | Verdict |
|--------|---------|
| R12 | **PARTIAL** |
| Human-Friendly Data Browser | **PARTIAL** |
| RAA Session Data Lifecycle | **NOT TESTED** |
| DSA ↔ RAA Coexistence | **NOT TESTED** |
| Equipment PI Assignment | **PARTIAL** |
| PI Charge Profile | **PARTIAL** |
| PI Pricing | **PARTIAL** |
| Production Deployment | **PASS** (infra); product qualification still **PARTIAL** |

## Anti-false-pass note

Live RAA E2E, DSA↔RAA concurrency, S3 upload/cleanup E2E, and live PI pricing E2E are **NOT** claimed PASS. Production deploy/migrate/smoke above are evidenced by Actions runs + EC2 `showmigrations` + public HTTP probes only.
