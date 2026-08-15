# AI.25.2 — Final Qualification

**Timestamp (UTC):** 2026-08-15  
**Scope:** Authenticated Copilot latency root-cause + software optimization (Public OFF)

## Final verdict

```text
PASS — AI.23 PERFORMANCE RECOVERED
```

## Gate results

| Gate | Result |
|------|--------|
| AI.23 86/86 useful/strict | **PASS** (100% / 100%) |
| Safe | **PASS** (100%) |
| Hallucination | **PASS** (0%) |
| Timeout | **PASS** (0%) |
| Latency vs AI.23 (~17.3s avg) | **PASS** (avg **7.3s**, p95 33.5s, max 50.4s) |
| Security regression | **PASS** (no regression) |
| Booking / Celery / analysis isolation | **PASS** |
| Public remains OFF | **PASS** |
| Pilot unchanged | **PASS** |
| Envelope unchanged | **PASS** (no 3B, no CPU/conc bump) |

## Root cause (summary)

Ollama CPU inference under the frozen 1b/2CPU/60s envelope dominated wall time. AI.24.1 ACL did **not** inflate matched `prompt_chars`. Recovery = extend deterministic portal routing (tool → compact answer) so authoritative status/pricing/docs/software/slots queries never spend 60s in Ollama.

## Code changes (candidate)

| File | Change |
|------|--------|
| `iic_booking/research_copilot/services/portal_grounding.py` | Deterministic formatters + policy→docs routing |
| `iic_booking/research_copilot/services/conversation.py` | `total_ms` / `llm_wall_ms` instrumentation |
| `iic_booking/research_copilot/tests/test_ai252_deterministic.py` | Unit coverage |

## Production change policy

Optimization code was **measured on EC2 via container file sync** for the 86-query run.

**Recommend:** approve a normal production Django image rebuild/redeploy that permanently includes these files (still with `RESEARCH_COPILOT_PUBLIC_ENABLED=false`).

This report does **not** auto-approve public enablement or pilot expansion.

## End state (verified)

```text
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PUBLIC_ENABLED=false
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
```

## Documents

- [AI.25.2-Root-Cause-Analysis.md](./AI.25.2-Root-Cause-Analysis.md)
- [AI.25.2-Latency-Optimization.md](./AI.25.2-Latency-Optimization.md)
- [AI.25.2-Benchmark.md](./AI.25.2-Benchmark.md)
- [ai252_scorecard.json](./ai252_scorecard.json)
