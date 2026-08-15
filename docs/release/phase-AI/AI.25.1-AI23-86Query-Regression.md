# AI.25.1 — AI.23 86-Query Regression on Deployed AI.24.1

**Primary gate:** FAIL  
**Artifact:** [`ai251_scorecard.json`](./ai251_scorecard.json)  
**Dataset:** `ai221_full_eval.json` (86 rows) — live Ollama on production Django after AI.24.1 sync  
**Pilot:** `test.student@iic-booking.test`  
**Public flag during run:** `RESEARCH_COPILOT_PUBLIC_ENABLED=false`  
**Run window (UTC):** ~2026-08-15 04:04 → ~05:04

## Absolute rule compliance

- All **86** queries executed against the **deployed** candidate (not unit tests, not inherited AI.23 JSON).
- No threshold weakening.
- No SQLite substitute.

## Scorecard vs AI.23 golden

| Metric | AI.23 golden | AI.25.1 on AI.24.1 | Gate |
|--------|--------------|-------------------|------|
| n | 86 | 86 | — |
| Useful-answer rate | **100%** | **61.6%** | FAIL |
| Strict success rate | **100%** (86/86) | **60.5%** (52/86) | FAIL |
| Safe-answer rate | **100%** | **100%** | PASS |
| Hallucination rate | **0%** | **0%** | PASS |
| Timeout rate | **0%** | **38.4%** (33/86) | FAIL |
| Avg latency | 17 317 ms | 41 816 ms | REGRESSED |
| p95 latency | 38 117 ms | 60 134 ms | REGRESSED |
| Max latency | 56 406 ms | 108 581 ms (multi-turn) | REGRESSED |

### Label counts (AI.25.1)

| Label | Count |
|-------|------:|
| CORRECT | 37 |
| TIMEOUT | 33 |
| NEEDS_CLARIFICATION | 8 |
| CORRECTLY_REFUSED | 7 |
| PARTIALLY_CORRECT | 1 |
| HALLUCINATION | 0 |
| SECURITY_FAILURE | 0 |

## Timeout inventory (exact query IDs)

`Q-A-003`, `Q-A-004`, `Q-B-002`, `Q-C-001`, `Q-D-002`, `Q-E-004`, `Q-F-001`, `Q-H-001b`, `Q-H-003`, `Q-I-002`, `Q-I-003`, `Q-J-003`, `Q-K-001`, `Q-L-001`, `Q-L-003`, `Q-M-001`, `Q-M-003`, `Q-N-001`, `Q-O-002`, `Q-O-003`, `Q-P-003`, `Q-P-004`, `Q-Q-001`, `Q-Q-002`, `Q-S-001`, `Q-S-002`, `Q-S-003`, `Q-T-001`, `Q-T-002`, `Q-U-003`, `Q-V-001`, `Q-V-002`, `Q-V-003`

Grading rule (same family as AI.23 eval harness): `llm_error_category in {timeout, provider_timeout}` **or** wall clock ≥ 58 000 ms → `TIMEOUT`.

## Regression classification

| Hypothesis | Assessment |
|------------|------------|
| Public/auth mode routing | Unlikely primary — run was authenticated-only with public OFF; security/refusal rows still green |
| Tool ACL | **Not implicated** — safe 100%, security probes refused, no `SECURITY_FAILURE` |
| Prompt/context bloat | Mixed — some category A prompts show similar `prompt_chars` to AI.23 yet still timeout; not a single smoking gun |
| Pricing / auth bugs | **Not implicated** for timeouts; pricing tools still invoked |
| Model change | **Ruled out** — still `llama3.2:1b` |
| Infrastructure / Ollama envelope saturation | **Primary** — Ollama held ~200% of 2-CPU limit during soak; p95 pinned at 60 s provider timeout; avg latency ~2.4× AI.23 |
| Eval harness contention | Contributing early risk — two overlapping eval processes were killed; final scorecard is from a **single** subsequent full pass that still timed out through late categories (S/T/U/V) |

**Conclusion:** Authenticated quality/latency regression under the frozen golden envelope. Do **not** raise concurrency/tokens/model to “pass” the gate in this task.

## Comparison note

AI.23 `ai23_scorecard.json` remains the golden baseline. AI.25.1 does **not** replace it.

## Gate decision

**BLOCKED — AUTHENTICATED REGRESSION**

Because this primary gate failed, AI.25.1 did **not** open the controlled public-enablement test window (`RESEARCH_COPILOT_PUBLIC_ENABLED=true`).
