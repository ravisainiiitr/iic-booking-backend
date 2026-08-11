# AI.15 — Research Copilot Live Pilot Qualification

**Date:** 2026-08-11  
**Mode:** AUTO MODE (qualification / controlled enablement — not feature development)  
**AI.14 baseline:** Backend `dc5433a` · Frontend `e9fa789` · Android `233740a`

## Final decision

**COPILOT NOT READY**

Live controlled pilot was **not** activated.

`RESEARCH_COPILOT_ENABLED` remains **false** on production.

Rationale (precise blockers):

1. **AI.14 is not deployed** to production (`INSTALLED_APPS_has_research_copilot=False`).
2. **`OPENAI_API_KEY_configured=False`** on production → Phase 3: **COPILOT LIVE PILOT = BLOCKED**.
3. **`RESEARCH_COPILOT_PILOT_EMAILS` not configured** (count=0).
4. **No authorized pilot account credentials** were available in this session (not invented).

AI.14 implementation readiness (**READY FOR LIMITED PRODUCTION PILOT**) still stands as code quality. AI.15 live qualification cannot claim pilot-active status without fabricating evidence.

---

## 1. AI.14 baseline

Portal-grounded tools, confirmation mutations, allowlist, 39 tests, frontend build — unchanged in this phase.

## 2. Deployment

| Item | Result | Evidence |
|------|--------|----------|
| Deploy AI.14 `dc5433a` / frontend `e9fa789` to production | **BLOCKED / NOT DONE** | Prod still lacks `research_copilot` app |
| Keep flag OFF during deploy | **PASS** (still OFF) | `RESEARCH_COPILOT_ENABLED=False` |
| Public capabilities | **PASS** | `research_copilot=false` |
| Unauthenticated portal smoke | **PASS** | `/api/version` 200, capabilities 200, analysis ready 200, equipments 200 |

Production deploy path remains the existing **Backend Release → Deploy Backend** tag workflow. AI.15 did **not** invent an alternate deploy or force-push production.

## 3. Migration status

**Before (Actions `31454498756` and `31454679807`):**

| Check | Result |
|-------|--------|
| `showmigrations research_copilot` | `No installed app with label 'research_copilot'.` |
| Core apps (communication, remote_analysis, device_provisioning, sync, equipment) | No pending migrations |
| `INSTALLED_APPS_has_research_copilot` | `False` |

**After:** unchanged — Copilot schema still absent (expected while undeployed / OFF).

No manual DB modification performed.

## 4. Secret / configuration status

From Show Production Migrations `31454679807` (booleans only — **no secret values printed**):

| Setting | Production |
|---------|------------|
| `RESEARCH_COPILOT_ENABLED` | `False` |
| `OPENAI_API_KEY_configured` | **`False`** |
| `RESEARCH_COPILOT_PILOT_EMAILS_configured` | **`False`** |
| `RESEARCH_COPILOT_PILOT_EMAILS_count` | `0` |
| `FCM_SERVER_KEY_configured` | `False` (unchanged; FCM not enabled) |

Local `.envs/.local/.django` has `RESEARCH_COPILOT_ENABLED=True` but **no** `OPENAI_API_KEY` name — local is not production.

## 5. Pilot allowlist

Not configured on production. Unit-tested in AI.14 (`test_pilot_allowlist_*`). Live allowlist verification **BLOCKED** (flag OFF + emails empty + no deploy).

## 6–23. Live E2E matrix areas

All controlled live E2E scenarios (basic chat, grounding, slots, wallet, cost, software, knowledge, isolation, booking confirmation, cancellation, results, RA, injection, audit of live mutations, frontend live UX) are:

**BLOCKED** — Live Copilot E2E blocked because:

- AI.14 not on production, and  
- `OPENAI_API_KEY` absent on production, and  
- no authorized pilot account was available.

No fabricated responses.

### Rate limiting

**PASS** (unit/config from AI.13/AI.14 — 60/hour chat, 30/hour tools). Live flood **NOT TESTED** (by design).

### Rollback procedure

**PASS** (operational verification of current state): backend flag OFF disables Copilot; capabilities `research_copilot=false`. Live flip ON→OFF exercise **NOT TESTED** (would require enablement first).

### Monitoring

Read-only Observability Sample `31454501144`: ready/version HTTP 200; recent log sample **5xx/ERROR count = 0**; nightly backup paths PRESENT. Copilot-specific request metrics N/A while OFF / undeployed.

### Android

Android has Copilot UI wired to the same backend bootstrap APIs (`CopilotScreen` / `CopilotRepository`).

**NOT PART OF CURRENT LIVE PILOT** — production backend capability is OFF / app not installed; no separate mobile enablement path was activated.

---

## Ops enablement checklist (when ready)

1. Cut Backend Release / Platform Release including AI.14 (`dc5433a` lineage) + frontend `e9fa789`.  
2. Deploy via existing Actions; run migrations so `research_copilot` installs.  
3. Set production secret `OPENAI_API_KEY` (never commit).  
4. Set `RESEARCH_COPILOT_PILOT_EMAILS=<authorized emails>`.  
5. Supply authorized pilot credentials securely to the operator session.  
6. Set `RESEARCH_COPILOT_ENABLED=true` (allowlist-only).  
7. Re-run AI.15 live E2E matrix; keep allowlist; do not go global.

Rollback: set `RESEARCH_COPILOT_ENABLED=false`.

---

## Read-only Actions added/used

| Run / artifact | Purpose |
|----------------|---------|
| PR #36 → master | AI15 readiness probe workflow + OpenAI/pilot boolean probe on Show Migrations |
| `31454679807` | Show Production Migrations **success** |
| `31454681607` | AI15 probe (partial; confirmed app not installed) |
| `31454501144` | Observability sample **success** |

---

## Final status table

| Area | PASS | PARTIAL | BLOCKED | NOT TESTED | Evidence |
|------|------|---------|---------|------------|----------|
| Deployment | | | X | | AI.14 not on prod |
| Migrations | X | | | | core OK; copilot app absent (expected) |
| OpenAI Configuration | | | X | | `OPENAI_API_KEY_configured=False` |
| Pilot Allowlist | | | X | | count=0 |
| Basic Chat | | | X | | no pilot / not enabled |
| Portal Grounding | | | X | | |
| Equipment Queries | | | X | | |
| Booking Queries | | | X | | |
| Slot Search | | | X | | |
| Wallet | | | X | | |
| Cost | | | X | | |
| Software Recommendation | | | X | | |
| Knowledge Search | | | X | | |
| Citations | | | X | | |
| User Isolation | | | X | | |
| Booking Confirmation | | | X | | |
| Cancellation | | | X | | |
| Result Security | | | X | | |
| Remote Analysis | | | X | | |
| Prompt Injection | | | X | | live; unit coverage remains from AI.13/14 |
| Rate Limiting | X | | | | AI.13/14 config + tests |
| Audit | | | X | | live mutations not run |
| Error Handling | | X | | | unit/fallback; live LLM fail NOT TESTED |
| Frontend UX | | | X | | live; build PASS in AI.14 |
| Android | | | | X | NOT PART OF CURRENT LIVE PILOT |
| Monitoring | X | | | | readiness 200; Copilot N/A while OFF |
| Rollback | X | | | | flag OFF verified backend-enforced |

---

## Remaining limitations

- Production still on pre-AI.14 Copilot packaging (app not installed).  
- No production OpenAI key.  
- No pilot allowlist / no controlled credentials in agent session.  
- Therefore: **do not enable** Copilot.

## SHAs (this phase)

| Repo | SHA | Notes |
|------|-----|-------|
| Backend feature tip | `7485aca` (+ docs commit) | AI.15 probe workflow on feature |
| Backend master | includes PR #36 readiness probes | |
| Frontend | `e9fa789` | unchanged |
| Android | `233740a` | unchanged |
