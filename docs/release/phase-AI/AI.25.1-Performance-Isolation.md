# AI.25.1 — Performance Isolation

**Date (UTC):** 2026-08-15

## Envelope (unchanged)

| Resource | Value |
|----------|-------|
| Model | `llama3.2:1b` |
| CPU | 2 |
| RAM | 8 GB |
| `RESEARCH_COPILOT_MAX_CONCURRENT` | 1 |
| `RESEARCH_COPILOT_MAX_TOKENS` | 160 |
| LLM timeout | 60 s |

No 3B install. No concurrency/token changes during qualification.

## Observations during 86-query soak

| Signal | Observation |
|--------|-------------|
| Ollama CPU | ~198–200% of container limit (fully saturated on 2 CPUs) |
| Ollama RAM | ~1.5 GiB / 8 GiB |
| Django | ~0.3–0.6% CPU, ~0.5–0.8 GiB |
| Celery worker | ping **pong** throughout; low CPU |
| Redis | up; analysis readiness `cache=ok` |
| Frontend | **200** |

## Portal isolation

| Check | During Copilot load / Ollama drill |
|-------|-------------------------------------|
| `/api/version/` | **200** |
| `/api/v1/analysis/health/ready/` | **200** |
| `/api/v1/analysis/health/live/` | **200** |
| `/api/equipments/` | **200** |
| Celery | healthy |
| Booking destructive actions | not exercised (non-destructive smoke only) |

Copilot latency regressed sharply vs AI.23, but **booking/Celery/analysis probes remained healthy**. Isolation priority (Booking > Celery > … > Copilot) held for portal availability.

## Ollama failure / recovery drill

| Step | Result |
|------|--------|
| Stop `iic-booking-backend-ollama-1` | done |
| Copilot response | Controlled unavailable: *“Research Copilot is temporarily unavailable. Your booking and other portal operations are unaffected…”* (`llm_error_category=network`, ~124 ms) |
| Frontend | **200** |
| Celery | **pong** |
| Analysis ready/live | healthy after recovery path confirmation |
| Start Ollama | recovered (`ollama list` OK) |
| Copilot after restart | Successful generation (~46 s for “What is FWHM?”) |
| DB repair | **not required** |

## Throttling soak

**NOT RUN** — requires temporary public enablement after a green AI.23 gate.

## Performance gate

| Question | Answer |
|----------|--------|
| Did Copilot materially break booking/Celery/analysis availability? | **No evidence** in probes |
| Did Copilot meet AI.23 latency/timeout quality? | **No** — see AI.25.1 86-query doc |

Primary release blocker remains **authenticated regression (timeouts)**, not a portal outage.
