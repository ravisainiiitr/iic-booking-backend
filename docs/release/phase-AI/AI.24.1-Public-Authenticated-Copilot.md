# AI.24.1 — Public + Authenticated Research Copilot

## Verdict

**PARTIAL — PUBLIC MODE READY, AUTHENTICATED REGRESSION NOT FULLY RE-RUN ON PROD**

Local implementation is complete for a single Research Copilot with two backend-enforced access modes. Production deploy is **not** performed. Full AI.23 86-query live regression and Postgres-backed pytest suite were **not** re-executed in this environment (local SQLite cannot migrate this schema; Docker Desktop unavailable). ACL smoke tests passed.

Do **not** treat this as production approval.

---

## Objective

Extend the **existing** Research Copilot (one UI, one LLM, one provider) with:

| Mode | Who | What |
|------|-----|------|
| **PUBLIC** | Anonymous visitors | Approved public knowledge, equipment catalogue, public pricing catalogue, public RA *description* |
| **AUTHENTICATED** | Logged-in (pilot rules) | Existing AI.23 private tools under existing authorization |

**Rule:** The LLM never decides authorization. The backend sets `access_mode` and enforces tool ACL before handlers / DB access.

---

## Architecture

```
RESEARCH COPILOT
       |
  request.user.is_authenticated + pilot allowlist
       |
  +----+----+
  |         |
PUBLIC   AUTHENTICATED
  |         |
public tools   all tools + ownership checks
```

### Access resolution

- `effective_access_mode(user)`:
  - anonymous → `public`
  - authenticated + pilot (or empty allowlist) → `authenticated`
  - authenticated but **not** on pilot → `public` (public tools only; private tools rejected)

### Settings

| Setting | Role |
|---------|------|
| `RESEARCH_COPILOT_ENABLED` | Master switch |
| `RESEARCH_COPILOT_PUBLIC_ENABLED` | Allow anonymous Public Copilot (default true when enabled) |
| `RESEARCH_COPILOT_PILOT_EMAILS` | Full authenticated private tools |
| `RESEARCH_COPILOT_ANON_THROTTLE` | Default `20/hour` per IP |
| `RESEARCH_COPILOT_ANON_TOOL_THROTTLE` | Default `15/hour` per IP |
| Golden Ollama envelope | **Unchanged** (`1b`, 2 CPU, 8 GB, concurrent 1, max_tokens 160) |

### Anonymous session

- Header: `X-Copilot-Anonymous-Key` (16–64 chars, opaque client id)
- Conversations: `user=NULL`, `anonymous_session_key`, `access_mode=public`
- Must **not** bind anonymous history to another authenticated user (frontend clears conversation on auth transition)

---

## Tool ACL classification

| Level | Tools |
|-------|--------|
| **PUBLIC** | `search_equipment`, `search_documentation`, `recommend_software`, `estimate_booking_cost` (EXTERNAL/STANDARD catalogue only) |
| **AUTHENTICATED** | `search_slots` |
| **AUTHORIZED_RESOURCE** | `search_bookings`, `get_next_booking`, `get_wallet`, `get_sample_status`, `get_booking_results`, `get_sample_deadline` |
| **MUTATION** | `create_booking`, `cancel_booking`, `create_support_ticket`, `launch_remote_analysis` |

Enforcement points:

1. Planner filter in `portal_grounding.run_portal_grounding`
2. `tools.execute_tool` ACL **before** handlers
3. Deterministic `private_intent_requires_login` in `send_message` / stream
4. Ownership checks inside authorized handlers (AI.13/AI.23)

---

## Public pricing policy

- Anonymous / public mode: catalogue estimate via EXTERNAL + STANDARD only (`public_catalogue=True`)
- Never PI / wallet-owner personalization without authentication
- If engine cannot complete: tell user to sign in / open booking calculate — **never invent a price**

---

## Frontend

- Single `ResearchCopilot` FAB — visible when Vite + backend enabled (no login required to open)
- Public banner + Sign in CTA → `/login` (no private conversation content in URL)
- Auth transition resets conversation id / history

## Android

**NOT IN SCOPE** for Public Copilot. Existing authenticated Android Copilot APIs remain; no parallel Android public implementation.

---

## Tests added

`iic_booking/research_copilot/tests/test_ai241_public_auth.py`

Covers: anonymous bootstrap, public Q, private Q → login, private tool reject, cross-user, ACL matrix, secrets refusal, anon key required, non-pilot forced public.

**Local evidence:** ACL smoke (import + tool ACL + private intent + strip infra) **PASS**. Full pytest requires Postgres-compatible CI/Docker.

---

## What was NOT done

- Production deploy
- Pilot expansion
- Installing `llama3.2:3b` / raising CPU/RAM/concurrency/MAX_TOKENS
- Live AI.23 86-query re-score on EC2
- Android public mode

---

## Rollback

1. Set `RESEARCH_COPILOT_PUBLIC_ENABLED=false` (authenticated pilot unchanged if ENABLED + allowlist)
2. Or set `RESEARCH_COPILOT_ENABLED=false`
3. Revert AI.24.1 commit(s) if needed; migration `0003_public_copilot_access` is additive (nullable user + anon key)

---

## Decision gate for production

Requires explicit approval after:

1. `migrate` + targeted pytest on Postgres
2. AI.23 86-query regression (safe 100%, hall 0%, timeout 0%)
3. Anonymous security matrix on staging/prod-like host
4. Frontend build verification
