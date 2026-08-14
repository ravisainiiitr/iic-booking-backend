# AI.22.1 — Quality Scorecard

**Date:** 2026-08-15 (IST)  
**Pilot:** `test.student@iic-booking.test` (unchanged)  
**Envelope:** `llama3.2:1b`, 2 CPU, 8 GB, concurrent 1, `MAX_TOKENS=160`  
**Machine-readable:** `ai221_scorecard.json` (same folder)

## Aggregate (n=86 graded rows)

Includes the original AI.22 taxonomy coverage plus AI.22.1 additions (PXRD-specific pricing, EXT, HAL probes).

| Metric | Value |
|--------|-------|
| CORRECT | **65** |
| NEEDS_CLARIFICATION | **9** (success) |
| CORRECTLY_REFUSED | **6** (success) |
| PARTIALLY_CORRECT | **4** |
| INCORRECT | **1** |
| TIMEOUT | **1** |
| HALLUCINATION | **0** |
| SECURITY_FAILURE | **0** |
| **Useful-answer rate** | **0.977** (84/86) |
| **Safe-answer rate** | **1.000** |
| **Hallucination rate** | **0.000** |
| **Timeout rate** | **0.012** |
| Avg wall | **18228 ms** |
| Max wall | **61117 ms** |

**Strict success** (CORRECT + NEEDS_CLARIFICATION + CORRECTLY_REFUSED) = **80/86 ≈ 93.0%**.  
Do **not** claim 99%.

## Category table

| Category | Queries | Correct | Clarify | Refuse | Partial | Incorrect | Timeout | Accuracy* |
|----------|---------|---------|---------|--------|---------|-----------|---------|-----------|
| A General science | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 100% |
| B Equipment info | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 100% |
| C Capability | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 100% |
| D Availability | 4 | 3 | 1 | 0 | 0 | 0 | 0 | 100% |
| E Booking | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 100% |
| F Modification | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 100% |
| G Cancellation | 2 | 1 | 1 | 0 | 0 | 0 | 0 | 100% |
| H Pricing | 6 | 4 | 1 | 0 | 1 | 0 | 0 | 83–100%† |
| I PI pricing | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 100%‡ |
| J Wallet | 3 | 2 | 0 | 1 | 0 | 0 | 0 | 100% |
| K Sample | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 100% |
| L Deadline | 3 | 2 | 0 | 0 | 1 | 0 | 0 | 67–100%† |
| M Results | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 100% |
| N Result location | 3 | 2 | 0 | 0 | 1 | 0 | 0 | 67–100%† |
| O Software | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 100% |
| P Remote analysis | 4 | 3 | 0 | 0 | 0 | 1 | 0 | 75% |
| Q RA data | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 100%§ |
| R Location | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 100% |
| S Policy | 3 | 2 | 0 | 0 | 1 | 0 | 0 | 67–100%† |
| T Documentation | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 100% |
| U Mixed | 3 | 2 | 0 | 0 | 0 | 0 | 1 | 67% |
| V Follow-up | 5 | 4 | 1 | 0 | 0 | 0 | 0 | 100% |
| W Ambiguous | 5 | 0 | 5 | 0 | 0 | 0 | 0 | 100% |
| X Security | 6 | 1 | 0 | 5 | 0 | 0 | 0 | 100% |
| EXT External-style | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 100% |
| HAL Hallucination probes | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 100% |

\*Accuracy = (CORRECT + NEEDS_CLARIFICATION + CORRECTLY_REFUSED) / n for the category.  
†Lower bound if PARTIAL counted as miss.  
‡PI **resolver/meta** verified; production has **0 PI ChargeProfiles** — live PI *amount* path **PARTIAL** (see Evaluation Completion).  
§Code/portal-level answers; live DSA/RAA still **BLOCKED — DNS**.

## Residual failures

| ID | Label | Notes |
|----|-------|-------|
| Q-P-002 | INCORRECT (pre-hotfix) | “Can I use it remotely?” without context — expected clarification; fix landed post-scorecard |
| Q-U-001 | TIMEOUT | Mixed cost+prepare hit ~60s once |
| Partials | tool mismatch / amount echo | Not security/hallucination |

## Tool-selection / clarification

- Ambiguous bare **XRD** for slots/pricing → deterministic **PXRD vs GI-XRD** clarify (**PASS**)
- Explicit **PXRD** / **GI-XRD** / **powder** / **grazing** ranking (**PASS**)
- Ambiguous book/cost/cancel pronouns → clarify (**PASS**)
- Cross-user results / wallet foreign selector / cancel confirm / injection → refuse (**PASS**)

## Performance

| | |
|--|--|
| Avg | ~18.2 s |
| Max | ~61 s (1 timeout) |
| Timeout rate | 1.2% |
| AI.21.2 envelope | preserved (no CPU/RAM/concurrency/model change) |
