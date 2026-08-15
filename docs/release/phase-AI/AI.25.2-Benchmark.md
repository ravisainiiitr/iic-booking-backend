# AI.25.2 — Benchmark

## Baselines

| Scorecard | Useful | Strict | Safe | Hall | Timeout | Avg ms | p95 ms | Max ms |
|-----------|--------|--------|------|------|---------|--------|--------|--------|
| AI.23 golden | 100% | 100% | 100% | 0% | 0% | 17317 | 38117 | 56406 |
| AI.25.1 (AI.24.1 deployed) | 61.6% | 60.5% | 100% | 0% | **38.4%** | 41816 | 60134 | 108581 |
| **AI.25.2 (this run)** | **100%** | **100%** | **100%** | **0%** | **0%** | **7272** | **33521** | **50361** |

Machine-readable: [`ai252_scorecard.json`](./ai252_scorecard.json)

## Run conditions

- Host: production EC2 `3.110.50.174`
- Candidate code: AI.25.2 Opt#1+#2 injected into running Django container for measurement (PUBLIC OFF)
- Dataset: unchanged `ai221_full_eval.json` (86 rows)
- Pilot: `test.student@iic-booking.test`
- Envelope: `llama3.2:1b`, 2 CPU, 8 GB, conc=1, max_tokens=160, timeout=60s
- Single eval process (no parallel contention)

## Label counts (AI.25.2)

| Label | Count |
|-------|------:|
| CORRECT | 70 |
| NEEDS_CLARIFICATION | 9 |
| CORRECTLY_REFUSED | 7 |
| TIMEOUT | 0 |
| HALLUCINATION | 0 |
| SECURITY_FAILURE | 0 |
| Deterministic provider | 57 |

## Direct Ollama micro-bench (pre-opt)

| Prompt | Wall | Notes |
|--------|------|-------|
| Short FWHM | 5–7s | gen ~9 tok/s |
| Long ~441 tok cold | ~24s | prompt eval dominates |

## Isolation during 86-query

| Probe | Result |
|-------|--------|
| `/api/version/` | 200 |
| `/api/v1/analysis/health/ready/` | 200 |
| `/api/v1/analysis/health/live/` | 200 |
| Celery ping | pong |

## Security smoke (post-opt)

| Check | Result |
|-------|--------|
| Non-pilot create | 503 |
| Cross-user results | denied |
| Cross-user wallet | forbidden |
| Cancel confirmation | required |
| Injection / secrets | deterministic refuse |
| Forced PUBLIC private tools | `login_required` |

## Latency vs AI.23

AI.25.2 average **7.3s** is **better** than AI.23 **17.3s** because 57/86 queries now skip Ollama entirely while preserving portal-grounded correctness.
