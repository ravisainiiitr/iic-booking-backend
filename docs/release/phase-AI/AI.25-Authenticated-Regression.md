# AI.25 — Authenticated Regression

## Decision on AI.23 86-query

**NOT EXECUTED against AI.24.1 code**

| Requirement | Evidence |
|-------------|----------|
| Dataset size | `ai221_full_eval.json` → **86 rows** (verified) |
| Golden baseline artifact | `ai23_scorecard.json` — useful/strict/safe **100%**, hall/timeout **0%**, avg ~17.3s |
| Live re-run on AI.24.1 | **Blocked by AI.25 deploy policy** — production Django image is pre-`b7f0fb3` |

Per absolute rule: **AI.23 regression cannot be marked PASS** without executing all 86 queries on the candidate build.

---

## What was verified for authenticated path (automated)

Postgres tests (`FakeInferenceProvider`):

| Check | Result |
|-------|--------|
| Authenticated private question path | **PASS** |
| Cross-user conversation denied | **PASS** |
| Wallet foreign selector denied (AI.13) | **PASS** |
| Mutating tools confirmation-only (AI.13) | **PASS** |
| Non-pilot authenticated forced public tools | **PASS** |
| Prompt injection rules present in system prompt | **PASS** |

These are **not** a substitute for the 86-query live Ollama evaluation.

---

## Pilot policy (unchanged)

- Account: `test.student@iic-booking.test`
- Do **not** expand allowlist in AI.25
- Do **not** change `RESEARCH_COPILOT_ENABLED`

---

## Required before enablement

On a build that includes `b7f0fb3` (and frontend `60cceaf`), with Ollama golden envelope:

```text
Useful = 100%
Strict = 100%
Safe = 100%
Hallucination = 0%
Timeout = 0%
```

Reuse `tmp_ai23_eval.sh` / AI.22.2 dataset, write `ai25_scorecard.json`.

If any row regresses: classify AI.24.1 vs environment vs pre-existing before changing golden behavior.
