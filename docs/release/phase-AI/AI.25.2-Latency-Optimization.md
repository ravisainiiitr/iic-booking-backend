# AI.25.2 — Latency Optimization Log

## Optimization #1 — Expand deterministic portal replies

**Change (one logical unit):** When authoritative portal tools already returned the answer, format a compact reply and **skip Ollama**.

Covers:

- `get_next_booking`, `get_wallet`, `get_sample_status`, `get_booking_results`, `get_sample_deadline`, `search_bookings`
- cost-only (`estimate_booking_cost` ± equipment identity)
- slots-only
- documentation / prepare snippets
- software catalogue

Excludes definitional/explanatory science questions (still LLM/RAG).

**Files:** `portal_grounding.py` (+ `total_ms` metadata in `conversation.py`)

### Representative bench (12 former hot queries)

| Metric | AI.25.1-like expectation | After Opt#1 |
|--------|--------------------------|-------------|
| Timeouts | many (subset of the 33) | **0 / 12** |
| Deterministic | low | **9 / 12** |
| Avg wall | ~40s+ | **~8.8s** |

## Optimization #2 — Route policy/FAQ phrases to `search_documentation`

**Change:** Expand docs planner needles (`policy`, `refund`, `lab access`, `cancellation`, `access hours`, `submission policy`) so policy questions become deterministic docs replies instead of raw LLM.

**Files:** `portal_grounding.py` (`plan_tool_calls` + `wants_prepare_docs`)

## Combined full regression (after #1 + #2)

See [AI.25.2-Benchmark.md](./AI.25.2-Benchmark.md) / `ai252_scorecard.json`.

| Metric | AI.25.1 | AI.25.2 |
|--------|---------|---------|
| Timeout rate | 38.4% | **0%** |
| Useful / Strict | 61.6% / 60.5% | **100% / 100%** |
| Avg wall | 41816 ms | **7272 ms** |
| Deterministic count | (few) | **57 / 86** |

## Explicitly not changed

- Ollama CPU/RAM/concurrency/model
- `MAX_TOKENS=160`
- Pilot allowlist
- Public flag (remains false)
- Booking / DSA / RAA architecture
- AI.23 eval dataset
