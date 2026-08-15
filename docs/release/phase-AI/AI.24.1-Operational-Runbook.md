# AI.24.1 — Operational Runbook

## Feature flags

```bash
# Master
RESEARCH_COPILOT_ENABLED=true

# Public anonymous mode (AI.24.1)
RESEARCH_COPILOT_PUBLIC_ENABLED=true

# Authenticated private tools (pilot)
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test

# Throttles
RESEARCH_COPILOT_USER_THROTTLE=60/hour
RESEARCH_COPILOT_TOOL_THROTTLE=30/hour
RESEARCH_COPILOT_ANON_THROTTLE=20/hour
RESEARCH_COPILOT_ANON_TOOL_THROTTLE=15/hour

# Frozen golden envelope — do not change without qualification
RESEARCH_COPILOT_MAX_CONCURRENT=1
RESEARCH_COPILOT_MAX_TOKENS=160
RESEARCH_COPILOT_LLM_TIMEOUT_SECONDS=60
```

## Migration

```bash
python manage.py migrate research_copilot
# applies 0003_public_copilot_access (nullable Conversation.user, anonymous_session_key, access_mode)
```

## Frontend

```bash
# Build-time soft gate still required
VITE_RESEARCH_COPILOT_ENABLED=true
```

Anonymous clients send `X-Copilot-Anonymous-Key` automatically when no auth token is present.

## Disable public mode only

```bash
RESEARCH_COPILOT_PUBLIC_ENABLED=false
# restart API
```

Authenticated pilot continues if `ENABLED=true` and email on allowlist.

## Full disable

```bash
RESEARCH_COPILOT_ENABLED=false
```

## Resource priority (unchanged)

Booking > Celery > DSA > RAA > Result processing > **Copilot** (lowest)

When Ollama busy: Copilot returns controlled busy response; do not raise concurrency for anonymous traffic.

## Monitoring

- Audit: public replies, `login_required`, tool denials (`login_required` / `forbidden`)
- Throttle 429s on anon scope
- Ollama CPU/RAM stay within 2 CPU / 8 GB envelope
- Confirm booking latency unaffected after any enablement

## Deploy policy

**Do not auto-deploy AI.24.1.** Require:

1. Postgres pytest `test_ai241_public_auth.py` green
2. AI.23 86-query regression green
3. Explicit production approval

## Android

Public Copilot **NOT IN SCOPE**. Leave authenticated Android path unchanged.

## Rollback order

1. `RESEARCH_COPILOT_PUBLIC_ENABLED=false`
2. If needed `RESEARCH_COPILOT_ENABLED=false`
3. Redeploy previous image / revert commit
4. Migration is additive — leaving columns in place is safe
