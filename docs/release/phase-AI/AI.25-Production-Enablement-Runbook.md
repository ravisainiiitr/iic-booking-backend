# AI.25 — Production Enablement Runbook

## Status

**NOT APPROVED YET** — AI.25 verdict is PARTIAL until AI.23 86-query runs on the AI.24.1 build.

This runbook describes the **future** controlled enablement procedure. Do not execute until an operator explicitly approves after a green AI.25→PASS follow-up.

---

## Preconditions

1. Backend image contains AI.24.1 (`b7f0fb3` or successor) + AI.25 migration fix
2. Frontend contains public Copilot UI (`60cceaf` or successor)
3. `migrate research_copilot` applied (`0003_public_copilot_access`)
4. AI.23 86-query scorecard green on that build
5. Anonymous security matrix green on that build
6. Ollama envelope unchanged: `llama3.2:1b`, 2 CPU, 8 GB, concurrent 1, max_tokens 160
7. Pilot still `test.student@iic-booking.test` only (unless separately approved)

---

## Recommended enablement order

### A. Deploy code with public OFF

```bash
# example — exact compose/env names follow production practice
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PUBLIC_ENABLED=false
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
RESEARCH_COPILOT_MAX_CONCURRENT=1
RESEARCH_COPILOT_MAX_TOKENS=160
```

Verify authenticated pilot still works; anonymous bootstrap should not expose private tools.

### B. Canary public ON

```bash
RESEARCH_COPILOT_PUBLIC_ENABLED=true
# restart API only
```

Immediately verify:

- anonymous public Q works
- anonymous private Q → Sign in
- private tools still 403/`login_required`
- booking `/api/version` + Celery healthy
- Ollama mem ≤ 8GiB

### C. Rollback public

```bash
RESEARCH_COPILOT_PUBLIC_ENABLED=false
# restart API
```

Authenticated pilot continues if `ENABLED=true`.

### D. Full disable

```bash
RESEARCH_COPILOT_ENABLED=false
```

---

## Throttles

| Scope | Default |
|-------|---------|
| `research_copilot_user` | 60/hour |
| `research_copilot_tool` | 30/hour |
| `research_copilot_anon` | 20/hour |
| `research_copilot_anon_tool` | 15/hour |

---

## Do not

- Expand pilot during public enablement without separate approval
- Raise concurrency/tokens/model size without measured qualification
- Treat `X-Copilot-Anonymous-Key` as authentication
- Force-push / overwrite unrelated R11–R14 / PI / DSA / RAA work
