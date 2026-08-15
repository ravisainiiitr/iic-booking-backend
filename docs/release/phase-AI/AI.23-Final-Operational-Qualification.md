# AI.23 — Final Operational Qualification

**Date:** 2026-08-15 (IST)  
**Host:** EC2 `3.110.50.174`  
**Question answered:** *Is Research Copilot safe to expand from one test account to a small controlled pilot?*  
**Not answered / out of scope:** *Is Copilot globally production-ready?* → **NO / not claimed**

**Machine-readable:** [`ai23_scorecard.json`](./ai23_scorecard.json)  
**Expansion plan:** [`AI.23-Pilot-Expansion-Plan.md`](./AI.23-Pilot-Expansion-Plan.md)

---

## Decision

**READY FOR PILOT CONTINUATION — OPERATIONAL BLOCKER REMAINS**

| Gate | Status |
|------|--------|
| Copilot 86-query quality | **PASS** (preserved / improved vs AI.22.2) |
| Safety / hallucination | **PASS** (100% / 0%) |
| Envelope freeze | **PASS** (unchanged) |
| Pilot allowlist isolation | **PASS** |
| Failure recovery | **PASS** |
| Core platform isolation | **PASS** |
| Production PI configuration | **BLOCKED / NOT CONFIGURED** (0 PI ChargeProfiles; 0 active EquipmentPI) |
| Live RAA / DNS | **BLOCKED — DNS** (`equip.iitr.ac.in` → `15.206.88.2` ≠ `3.110.50.174`) |
| Global enablement | **NO** |
| Automatic pilot expansion | **NOT PERFORMED** |

**Interpretation:** Copilot is **safe to continue** on the single controlled test account and is **quality-ready** for a *future* small allowlist expansion after human approval. **Do not expand yet** while production PI configuration and live RAA DNS remain operational blockers for those domains. Expansion plan is prepared separately — not executed.

---

## Envelope freeze (verified live)

| Setting | Expected (AI.22.2) | Live AI.23 | Match |
|---------|-------------------|------------|-------|
| Model | `llama3.2:1b` | `llama3.2:1b` (ollama list) | YES |
| Ollama CPU | 2 | NanoCpus=`2000000000` | YES |
| Ollama RAM | 8 GB | Memory=`8589934592` | YES |
| `RESEARCH_COPILOT_MAX_CONCURRENT` | 1 | `1` | YES |
| `RESEARCH_COPILOT_MAX_TOKENS` | 160 | `160` | YES |
| LLM timeout | 60s | `60` | YES |
| Enabled | true | `true` | YES |
| Pilot emails | `test.student@iic-booking.test` | same | YES |
| Provider | ollama | `COPILOT_PROVIDER=ollama` | YES |

**No envelope changes were made during AI.23.**

---

## Production posture

| Item | Value |
|------|-------|
| Global Copilot | **NO** (allowlist only) |
| Pilot | `test.student@iic-booking.test` |
| PI ChargeProfiles (`pricing_profile=pi`) | **0** |
| Active EquipmentPI | **0** |
| DNS `equip.iitr.ac.in` | **15.206.88.2** |
| EC2 public IP | **3.110.50.174** |
| `/api/version` | HTTP 200 (`portal_version` 2.5.2) |
| Analysis ready/live | HTTP 200 |

---

## PI qualification (separated)

| Layer | Result |
|-------|--------|
| CODE QUALIFICATION | **PASS** — `pi_pricing` resolver, wallet-owner identity, fallback when no PI profiles, estimate tool exposes `pricing_resolution` meta |
| TEST DATA QUALIFICATION | **PASS** (6/7 matrix rows) — ephemeral atomic transaction **rolled back**; production left at 0 PI profiles / 0 EquipmentPI |
| PRODUCTION CONFIGURATION | **BLOCKED / NOT CONFIGURED** |

### PI test matrix (ephemeral, rolled back)

| Case | Result |
|------|--------|
| Production pilot live (no invent) | **PASS** → `standard`, `equipment_has_pi_profiles=False` |
| 1 Normal user | **PASS** → not PI |
| 2 Equipment PI + PI profiles | **PASS** → `pi` |
| 4 Wallet owner PI (current user not PI) | **PASS** → `pi` via billing identity |
| 5 Neither PI | **PASS** → `standard` |
| PI identity but inactive PI profiles | **PASS** → fallback away from `pi` |
| Estimate tool under PI meta | **PARTIAL** — resolver `pi` + `billing_pi=true`, but **amount null** on incomplete ephemeral PI ChargeProfile clone (engine did not invent a number) |

**LLM never decides PI status.** Server-side resolver remains authoritative.  
**No production PI rows were kept** (`after` counts = 0).

---

## DNS / RAA

| Check | Result |
|-------|--------|
| Read-only DNS | `equip.iitr.ac.in` → **15.206.88.2** |
| Expected for live RAA on this host | **3.110.50.174** |
| LIVE RAA QUALIFICATION | **BLOCKED — DNS** |
| Live session E2E | **Not claimed** |
| Code-level RA/software questions in 86-set | Exercised (portal/software tools) |

DNS was **not** modified.

---

## Security regression

| Check | Result |
|-------|--------|
| Pilot allowlisted | PASS |
| Non-pilot denied | PASS |
| Unauthenticated `feature_enabled` | PASS (False) |
| Cross-user results/sample | PASS (denied / not found) |
| Cancel confirmation | PASS |
| System prompt / API keys / Ollama URL | PASS (deterministic refusal) |
| “Another user results/wallet” phrasing | Soft miss in initial ops suite → **hardened** with `security_refusal` needles; post-hotfix **PASS** (deterministic) |
| 86-set category X | CORRECTLY_REFUSED paths held |
| Hallucination probes (missing ids) | PASS (deterministic not-found) |

**Acceptance:** no security failure requiring `RESEARCH_COPILOT_ENABLED=false`.

---

## Failure recovery

| Scenario | Portal `/api/version` | Analysis ready | Copilot |
|----------|----------------------|----------------|---------|
| Ollama stopped | **200** | **200** | Controlled error (`network`) — “temporarily unavailable… booking unaffected” |
| Ollama restarted | **200** | **200** | Recovered |
| Busy gate (`MAX_CONCURRENT=1`) | — | — | Second acquire → `CopilotBusyError` **PASS** |
| Rollback flag semantics | — | — | `RESEARCH_COPILOT_ENABLED=false` disables allowlist user (**override test**; live flag left **true**) |

---

## Core platform isolation

- Clarification / deterministic paths: ~tens of ms; PostgreSQL `SELECT 1` OK after turn.  
- Inference not held inside long DB transactions (message writes are short atomics).  
- Priority unchanged: Booking > Celery > DSA > RAA > Result processing > Copilot.  
- Copilot failure (Ollama down) did **not** take down version/ready endpoints.

---

## 86-query regression (AI.22.2 dataset, unchanged)

| Metric | AI.22.2 | AI.23 |
|--------|---------|-------|
| n | 86 | 86 |
| Useful-answer rate | 100% | **100%** |
| Strict success | 85/86 ≈ 98.8% | **86/86 = 100%** |
| Safe-answer rate | 100% | **100%** |
| Hallucination rate | 0% | **0%** |
| Timeout rate | 0% | **0%** |
| Avg wall | ~16692 ms | **17317 ms** |
| p95 | ~38718 ms | **38117 ms** |
| Max | ~48917 ms | **56406 ms** |

Counts: CORRECT 70 · NEEDS_CLARIFICATION 9 · CORRECTLY_REFUSED 7 · TIMEOUT 0 · HALLUCINATION 0.

Small latency variation vs AI.22.2 is acceptable (post-Ollama restart / load). **No dataset edits.** Q-U-001 smoke after hotfix: **~588 ms**, deterministic.

---

## Monitoring recommendations

Capture: request count, success/fail, timeout, busy, tool failures, latency, Ollama up/down.  
**Do not** log API keys, passwords, tokens, or unnecessary private payloads.

| Threshold | Action |
|-----------|--------|
| Timeout rate > 2% | Warn / investigate |
| Error rate > 2% | Warn / investigate |
| Any verified hallucination | Investigate immediately |
| Any security failure | **Pause pilot** (`RESEARCH_COPILOT_ENABLED=false`) |

---

## Rollback

```text
RESEARCH_COPILOT_ENABLED=false
```

Optional: `docker stop` Ollama container.  
Does **not** require rollback of Booking, DSA, RAA, Celery, or result processing.  
Verified: flag override disables Copilot; live pilot left enabled for continued single-account pilot.

---

## Model / resource decision

**Do not** install `llama3.2:3b`. **Do not** raise CPU/RAM/concurrency/`MAX_TOKENS`. AI.22.2 quality baseline preserved under the frozen envelope.

---

## Final answer to the expansion question

> Is Research Copilot safe to expand from one test account to a small controlled pilot?

**Conditionally yes on quality/safety grounds — but not yet operationally cleared for expansion.**

- Continue current pilot: **YES**  
- Expand allowlist now: **NO** (await human approval + prefer clearing PI config and/or DNS, or accept documented domain caveats in the expansion plan)  
- Enable globally: **NO**
