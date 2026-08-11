# AI.18 — Controlled Pilot Final Report

**Date:** 2026-08-11  
**Final verdict:** **PARTIAL — BLOCKED**

## What completed

| Item | Status |
|------|--------|
| Production audit | Done — see AI.18-Production-Integration-Assessment.md |
| Surgical merge to master | **MERGED** PR #64 → `3b6d76a` |
| Frontend UX | **MERGED** PR #7 |
| Android messaging | **MERGED** PR #1 |
| Release tag | `v2.5.20-ai18-research-copilot-off` created |
| Provider unit tests | 12 passed |
| Frontend build | PASS |
| Android `gradlew test` | PASS |

## What blocked

| Item | Status |
|------|--------|
| Deploy AI.18 tag | **FAILED** health (`connection refused` on :8080); rolled back |
| Concurrent R11 catalog deploys | Interfered; prod now on `v2.5.5-r11-catalog-sync.2` |
| research_copilot on live prod | **Still absent** (post-rollback pointer) |
| Migrations 0001/0002 on prod | **NOT APPLIED** (app not installed) |
| EC2 CPU/RAM/GPU numbers | **NOT MEASURED** (host probe pending clean window) |
| Ollama on prod | **NOT INSTALLED** (correct — resources unqualified) |
| Pilot allowlist / enablement | **NOT DONE** — no inventable accounts; flag must stay false |
| Authenticated Copilot E2E | **NOT RUN** |

## Safety

`RESEARCH_COPILOT_ENABLED` remains **false**. Booking/DSA/RAA remain independent of Ollama.
