# AI.21 — Controlled Research Copilot Production Pilot Report

**Date:** 2026-08-14 / 2026-08-15 IST  
**Host:** `3.110.50.174` (`ip-10-0-1-153`)  
**DNS:** `equip.iitr.ac.in` → `15.206.88.2` (**unchanged; not modified**)  

## FINAL VERDICT

### Prior (emails missing): **PILOT BLOCKED — AUTHORIZED PILOT EMAILS NOT PROVIDED**

### Update (operator authorized seeded test email): **LIMITED PILOT ACTIVE (test account only)**

See addendum: [AI.21.1-Test-Pilot-Enablement.md](./AI.21.1-Test-Pilot-Enablement.md)

- Allowlist: `test.student@iic-booking.test` only  
- Flag: `RESEARCH_COPILOT_ENABLED=true`  
- Non-pilot (`test.faculty@…`) denied  
- Not global production ready  

---

## Phase results

### Phase 1 — AI.20 deployment

| Check | Result | Evidence |
|-------|--------|----------|
| AI.20 markers in running image | **PASS** | `resolved_equipment_id` present after rebuild |
| Durable deploy (not only hot-copy) | **PASS** | Host tree synced → `docker compose … build django` → recreate |
| Image built | **PASS** | `iic_booking_production_django` sha256 `961328bd…` |
| Container recreated | **PASS** | Django recreated 2026-08-14 ~19:47 UTC |
| Health after recreate | **PASS** | `/api/version` 200; `/api/v1/analysis/health/ready/` 200 |
| Unrelated R12/R14/PI deploy | **PASS** | Django-only rebuild; no DSA/RAA/DNS changes |
| Repo commit on EC2 checkout | **PARTIAL** | Deploy tree HEAD still `20321ff` (R13); AI.20 files synced into tree + baked into image |

Local AI.20 commit reference: `2ff6526` (`fix(ai20): harden Copilot portal search/pricing grounding…`).

### Phase 2 — Ollama

| Check | Result | Evidence |
|-------|--------|----------|
| Service | **PASS** | `iic-booking-backend-ollama-1` Up |
| Version | **PASS** | `0.32.11` |
| Model | **PASS** | `llama3.2:1b` (1.3 GB) |
| CPU / RAM limits | **PASS** | 2.0 CPU / 8 GiB (unchanged) |
| Concurrent | **PASS** | `MAX_CONCURRENT=1` |
| Port 11434 public | **PASS** | not listening; ports null |
| Provider health | **PASS** | `OllamaGateway` available |
| Small smoke | **PASS** | complete ~2.6 s (preflight) |

**No** 3b install. **No** limit increases.

### Phase 3 — Disabled state

| Check | Result | Evidence |
|-------|--------|----------|
| `RESEARCH_COPILOT_ENABLED` | **PASS** | `false` in env + container |
| Bootstrap `enabled` | **PASS** | `False` |
| API gate | **PASS** | 503 `research_copilot_disabled` |
| Frontend/Android live disabled UI | **NOT TESTED** | no pilot login session; code paths already respect backend `enabled` (AI.20) |

### Phase 4–5 — Allowlist + enablement

| Check | Result | Evidence |
|-------|--------|----------|
| Real pilot emails provided by operator | **NO** | none in this session / prior AI docs |
| `RESEARCH_COPILOT_PILOT_EMAILS` | **empty** | count = **0** |
| `RESEARCH_COPILOT_ENABLED=true` | **NOT DONE** | correctly withheld |

### Phases 6–21 — Live pilot matrix

All live pilot / frontend-pilot / Android-pilot / security-with-two-accounts / mutation-with-confirm / observation items:

**BLOCKED** or **NOT TESTED** — blocked by missing authorized pilot emails (and therefore no enablement).

AI.20 service-layer evidence remains the last functional qualification for tools/grounding/security (see AI.20 reports). AI.21 does **not** re-claim those as live HTTP pilot E2E.

### Phase 22 — DNS

**PASS (constraint obeyed):** DNS not modified; DSA/RAA hostname architecture unchanged. Live DSA/RAA under Copilot remains **BLOCKED BY DNS**.

### Phase 23 — Rollback

| Check | Result |
|-------|--------|
| Primary rollback already active | **PASS** (`ENABLED=false`) |
| Ollama stop available | **PASS** (procedure unchanged; not required this phase) |

---

## Acceptance matrix

| Item | Status |
|------|--------|
| AI.20 release deployed | **PASS** (durable Django rebuild with AI.20 files) |
| Ollama healthy | **PASS** |
| llama3.2:1b healthy | **PASS** |
| Feature flag verified OFF | **PASS** |
| Real pilot allowlist configured | **BLOCKED** |
| Pilot/non-pilot isolation (live enabled) | **BLOCKED** |
| Live pilot login | **BLOCKED** |
| Live Copilot chat | **BLOCKED** |
| Portal grounding (live pilot HTTP) | **BLOCKED** |
| Booking / slots / pricing / PI / sample / results / software (live pilot) | **BLOCKED** |
| Knowledge / general / mixed (live pilot) | **BLOCKED** |
| User authorization / cross-user (live pilot) | **BLOCKED** |
| Review & Confirm (live pilot) | **BLOCKED** |
| Prompt injection (live pilot) | **BLOCKED** |
| Tool failures / Ollama failure / timeout / concurrency (live this phase) | **NOT TESTED** (use AI.19/AI.20 evidence; no enablement) |
| Booking performance / Celery / result processing (pilot-on) | **NOT TESTED** |
| Frontend live / Android live | **BLOCKED** |
| Audit (pilot-on) | **BLOCKED** |
| Resource monitoring (pilot-on) | **NOT TESTED** |
| Rollback | **PASS** (remains OFF) |
| Pilot observation | **BLOCKED** — see observation stub |

---

## Why enablement was correctly refused

AI.21 rules require:

> ONLY with real authorized email addresses explicitly provided by the operator.

This session supplied **zero** such emails. Configuring allowlist from admin/user DB rows would be **inferring** emails — also forbidden.

Therefore the only evidence-based outcome is:

**PILOT BLOCKED — AUTHORIZED PILOT EMAILS NOT PROVIDED**

---

## Operator next steps (to reach limited pilot)

1. Explicitly provide authorized pilot email(s) for `RESEARCH_COPILOT_PILOT_EMAILS`
2. Keep model/resources: `llama3.2:1b`, 2 CPU, 8 GB, concurrent 1
3. Set allowlist, verify non-pilot denied **before** broad use
4. Set `RESEARCH_COPILOT_ENABLED=true` (allowlist-only)
5. Execute AI.21 Phases 6–20 live matrix with those accounts
6. Keep DNS migration separate

Until step 1: leave Copilot **OFF**.
