# AI.25.3 — Final Qualification

**Timestamp (UTC):** 2026-08-15T08:09Z

## Final verdict

```text
PASS — AI.25.3 PRODUCTION DEPLOYED AND VERIFIED
```

## Acceptance matrix

### DEPLOYMENT

| Item | Result |
|------|--------|
| Qualified source deployed | **PASS** (`3a72438`) |
| Django image contains AI.25.2 | **PASS** (baked + marker file) |
| Version/SHA verified | **PASS** (`BACKEND_GIT_COMMIT=3a72438…`, tag `v2.5.41-ai25.3-copilot-deterministic`) |
| Production health | **PASS** |

### COPILOT

| Item | Result |
|------|--------|
| Authenticated pilot | **PASS** |
| Deterministic routing | **PASS** |
| Portal grounding | **PASS** |
| General LLM path | **PASS** |
| Pricing authority | **PASS** |
| Security | **PASS** |
| Cancellation confirmation | **PASS** |

### PERFORMANCE

| Item | Result |
|------|--------|
| Deterministic latency | **PASS** (~14–115 ms) |
| LLM latency | **PASS** (~53s explanation, no timeout) |
| CPU/RAM isolation | **PASS** |
| Booking health | **PASS** |
| Celery health | **PASS** |
| Analysis health | **PASS** |

### CONFIGURATION

| Item | Result |
|------|--------|
| Public Copilot OFF | **PASS** |
| Pilot unchanged | **PASS** |
| Ollama envelope unchanged | **PASS** |
| No 3B | **PASS** |
| No concurrency increase | **PASS** |

## Final production state

```text
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PUBLIC_ENABLED=false
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
OLLAMA: llama3.2:1b / 2 CPU / 8 GB / MAX_CONCURRENT=1 / MAX_TOKENS=160
```

No public enablement. No pilot expansion. No DNS/RAA/DSA/PI changes.

## Documents

- [AI.25.3-Production-Deployment.md](./AI.25.3-Production-Deployment.md)
- [AI.25.3-Production-Smoke.md](./AI.25.3-Production-Smoke.md)
- [AI.25.3-Deterministic-Routing-Verification.md](./AI.25.3-Deterministic-Routing-Verification.md)
