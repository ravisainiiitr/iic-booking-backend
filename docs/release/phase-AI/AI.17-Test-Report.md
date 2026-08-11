# AI.17 — Test Report

**Date:** 2026-08-11

## Backend

Target:

```text
pytest iic_booking/research_copilot/tests
```

Coverage includes:

- AI.1 conversation
- AI.2 knowledge
- AI.3 tools
- AI.13 security / injection / isolation
- AI.14 portal grounding functional
- AI.17 Ollama provider, FakeInferenceProvider, COPILOT_PROVIDER alias, concurrency busy path, graceful Ollama-down

Evidence log: `docs/release/phase-AI/ai17-complete-pytest.log`

**Result (this session):** **54 passed** in ~107s (Docker + test Postgres).

## Frontend

- `ResearchCopilot` quick actions expanded to portal-grounded prompts
- Busy/unavailable error copy
- Backend `enabled` remains authoritative

**Result (this session):** `npm run build` **PASS** (~16s). Log: `ai17-frontend-build.log`.

## Android

- Bootstrap respects backend disable
- Unavailable copy clarifies portal remains usable
- Build: `./gradlew test assembleDebug assembleRelease` — **NOT TESTED** this session (deferred; code changes are minimal copy-only)

## Real Ollama E2E

| Step | Status |
|------|--------|
| Ollama installed | Yes (`0.32.9`) on this host |
| Model pulled | Yes — `llama3.2:1b` (1.3 GB) |
| Direct `/api/chat` smoke | **PASS** — reply `ok`, ~33013 ms |
| Full portal-grounded live chat against Django | **NOT TESTED** (would require enabled flag + auth fixtures) |

Do not invent production latency or quality scores.
