# AI.24 — Final Qualification Report

**Date:** 2026-08-15 (IST)  
**Host:** EC2 `3.110.50.174`  
**Golden baseline:** AI.23 (`ai23_scorecard.json`)  
**Companions:**  
[`AI.24-Pilot-Expansion-Qualification.md`](./AI.24-Pilot-Expansion-Qualification.md) ·  
[`AI.24-Operational-Runbook.md`](./AI.24-Operational-Runbook.md)

---

## Decision

**READY FOR PILOT CONTINUATION — OPERATIONAL BLOCKER**

| Question | Answer |
|----------|--------|
| Is Copilot core quality/safety still golden? | **YES** |
| Is global enablement in scope? | **NO** |
| Were pilot users added in AI.24? | **NO** |
| Can allowlist expand to 3–5 *after explicit approval*? | Procedure **READY** — execution **NOT PERFORMED** |
| Block expansion solely because PI/DNS incomplete? | **No for Copilot core** — but formal AI.24 status remains **operational blocker** per Phase 14 Option B |

### Explicit statuses

| Area | Status |
|------|--------|
| Copilot quality (AI.23 86-set) | **PASS** (not re-run full 86; smoke + golden envelope verified) |
| Copilot safety / authorization | **PASS** |
| Hallucination (baseline) | **PASS** (0%) |
| Timeout (baseline) | **PASS** (0%) |
| Ollama health / envelope freeze | **PASS** |
| Latency (baseline) | **PASS** (~17.3 s avg; smoke Q-U-001 ~108 ms deterministic) |
| Pricing resolver | **PASS** |
| Production PI configuration | **NOT CONFIGURED** / **BLOCKED** |
| DNS | **BLOCKED** (stale A record) |
| Live RAA | **BLOCKED — DNS** |
| Booking / Celery / analysis impact | **PASS** (no adverse signal) |
| Pilot allowlist isolation | **PASS** |
| Rollback | **PASS** |
| Global enablement | **NOT TESTED as enablement** — remains **disabled by policy** |
| Pilot expansion execution | **NOT TESTED / NOT EXECUTED** |

---

## Golden baseline (AI.23) — preserved

| Metric | AI.23 | AI.24 drift check |
|--------|-------|-------------------|
| Useful | 100% | Envelope + smoke **PASS** |
| Strict | 100% (86/86) | Not re-run full 86 (no evidence of drift) |
| Safe | 100% | Security smoke **PASS** |
| Hallucination | 0% | Not-found / refusal paths **PASS** |
| Timeout | 0% | Smoke deterministic paths **PASS** |
| Avg latency | ~17.3 s | Unchanged envelope |

### Envelope freeze (live)

| Setting | Required | Live | Status |
|---------|----------|------|--------|
| Model | `llama3.2:1b` | `llama3.2:1b` | **PASS** |
| CPU | 2 | NanoCpus `2000000000` | **PASS** |
| RAM | 8 GB | `8589934592` | **PASS** |
| `MAX_CONCURRENT` | 1 | `1` | **PASS** |
| `MAX_TOKENS` | 160 | `160` | **PASS** |
| `RESEARCH_COPILOT_ENABLED` | true | true | **PASS** |
| Pilot emails | single test student | `test.student@iic-booking.test` | **PASS** |
| `ALL_GOLDEN` | — | **True** | **PASS** |

**No envelope changes in AI.24. No 3B. No resource increase.**

---

## Current pilot

```text
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
```

| Check | Result | Status |
|-------|--------|--------|
| Pilot allowed | `feature_enabled(pilot)=True` | **PASS** |
| Non-allowlisted (`iicbooking@iitr.ac.in`) | `False` | **PASS** |
| Unauthenticated API | HTTP **401** (`Authentication credentials were not provided.`) | **PASS** |
| Global unrestricted access | Allowlist non-empty → denied for others | **PASS** |

---

## Smoke regression (AI.24)

| Case | Result | Status |
|------|--------|--------|
| Q-U-001 cost+prepare | ~108 ms, deterministic, portal amount | **PASS** |
| Ambiguous cost | Clarification | **PASS** |
| Ollama URL / secrets | Security refusal | **PASS** |
| Another-user results/wallet phrasing | Security refusal | **PASS** |
| Cross-user booking tools | `booking_not_found` / denied | **PASS** |
| Rollback override `ENABLED=false` | Disables pilot | **PASS** |

---

## Platform health

| Probe | Result | Status |
|-------|--------|--------|
| `/api/version` | HTTP 200 | **PASS** |
| `/api/v1/analysis/health/ready/` | HTTP 200 `ready` | **PASS** |
| `/api/v1/analysis/health/live/` | HTTP 200 | **PASS** |
| Celery inspect ping | `pong` / 1 node online | **PASS** |
| Django / Redis containers | Up (healthy) | **PASS** |
| Ollama container | Up; model listed; Django→`ollama:11434` (host :11434 not published — expected) | **PASS** |

Ollama stop/restart failure recovery: **PASS** evidence from AI.23 (not re-executed in AI.24 to avoid unnecessary disruption).

---

## PI

| Layer | Status |
|-------|--------|
| Implementation / resolver | **PASS** |
| Production PI ChargeProfiles | **0 — NOT CONFIGURED** |
| Active EquipmentPI | **0 — NOT CONFIGURED** |
| Pilot live resolution | `standard` (`equipment_has_pi_profiles=False`) | **PASS** (correct fallback) |

**No production PI data invented.**

---

## DNS / RAA

| Item | Value | Status |
|------|-------|--------|
| `equip.iitr.ac.in` | `15.206.88.2` | **BLOCKED** (stale) |
| EC2 public IP | `3.110.50.174` | — |
| Live RAA E2E | Not run | **BLOCKED — DNS** |
| DNS modified by AI.24 | No | — |

---

## Separation (Phase 13)

```text
COPILOT CORE
  intelligence / safety / pricing resolver / auth / Ollama  → PASS
PRODUCTION PI CONFIG                                       → NOT CONFIGURED
LIVE RAA                                                   → BLOCKED — DNS
```

Copilot implementation is **not** failed due to PI/DNS absence.

---

## Expansion recommendation

1. **Continue** single-account pilot (`test.student@iic-booking.test`).  
2. **Do not** enable globally.  
3. **Do not** auto-add users.  
4. Expansion to **3–5** emails is **procedurally ready** (see expansion qualification + runbook) pending **explicit admin approval** of each email and acceptance of PI/RAA caveats.  
5. Formal AI.24 gate status: **READY FOR PILOT CONTINUATION — OPERATIONAL BLOCKER** (PI + DNS).

---

## Non-goals confirmed

- No new model / 3B / provider  
- No Ollama CPU/RAM/concurrency/token increases  
- No new pricing / booking / RAA engines  
- No wildcard allowlist  
- No invented users or PI rows  
- No RAA PASS claim while DNS stale
