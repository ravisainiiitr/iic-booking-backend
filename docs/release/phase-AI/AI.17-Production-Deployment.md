# AI.17 — Production Deployment & Pilot Procedure

**Date:** 2026-08-11  
**Default posture:** `RESEARCH_COPILOT_ENABLED=false`

## Deploy order (safe)

1. Deploy backend with Copilot code + **flag OFF**
2. Apply `research_copilot` migrations (AI.16 workflow if needed)
3. Verify portal health (`/api/v1/analysis/health/ready/`, booking smoke)
4. Inspect EC2 CPU/RAM/disk/GPU **read-only** before installing Ollama
5. Install/configure Ollama **separately** (prefer private host or heavily capped container)
6. Pull approved model once (`ollama pull <model>`) — do not auto-pull on every deploy
7. Set env: `COPILOT_PROVIDER=ollama`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, concurrency/timeouts
8. Verify staff LLM health endpoint (no secrets exposed)
9. Deploy frontend (Vite flag optional; backend remains authoritative)
10. Verify Android uses production API; Copilot hidden when backend disabled
11. Keep flag **OFF**; run disabled-state regression
12. Configure `RESEARCH_COPILOT_PILOT_EMAILS`
13. Enable only for pilot → controlled live pilot → monitor → decide broader enablement

## Rollback

```
RESEARCH_COPILOT_ENABLED=false
```

Optionally stop Ollama independently. No booking/DSA/RAA path may depend on Ollama.

## Secrets / env (production)

Required for Copilot inference path:

- `COPILOT_PROVIDER=ollama`
- `OLLAMA_BASE_URL=<private>`
- `OLLAMA_MODEL=<approved>`

Do **not** require `OPENAI_API_KEY`.

Keep:

- `RESEARCH_COPILOT_ENABLED=false` until pilot
- `RESEARCH_COPILOT_PILOT_EMAILS=<comma emails>` before enablement

## Resource guidance (before co-locating Ollama)

| Host size | Guidance |
|-----------|----------|
| Small shared Django/Postgres/Redis | Prefer **separate** AI host; do not co-locate large models |
| Medium (≥8 vCPU, ≥16 GB RAM free) | CPU 3B model with `MAX_CONCURRENT=1–2`, mem/cpu caps |
| GPU present | Verify GPU before enabling; still cap concurrency |

Exact production EC2 measurements must be recorded from a live read-only probe — do not invent.

## Pilot checklist

- [ ] Flag false deploy healthy
- [ ] Ollama reachable privately
- [ ] Model pulled and health `available`
- [ ] Allowlist configured
- [ ] Pilot user can chat; non-pilot denied
- [ ] Booking create/cancel unaffected under Copilot load
- [ ] Celery/DSA/RAA unaffected
- [ ] Rollback to flag false verified
