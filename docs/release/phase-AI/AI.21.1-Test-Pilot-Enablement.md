# AI.21.1 — Seeded Test Pilot Enablement Addendum

**Date:** 2026-08-14 / 2026-08-15 IST  
**Trigger:** Operator instruction to use the existing seeded test email for testing.

## Pilot identity (existing seed only)

| Field | Value |
|-------|-------|
| Email | `test.student@iic-booking.test` |
| Source | Production `seed_test_users` / AI.7 controlled test account |
| User id | 76 |
| `is_test_account` | yes (seeded) |
| Passwords | **not documented / not invented** |

Non-pilot control account (not allowlisted): `test.faculty@iic-booking.test` (id 78).

## Production configuration applied

```text
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
```

Ollama envelope **unchanged**: `llama3.2:1b`, 2 CPU, 8 GB, `MAX_CONCURRENT=1`.

Django recreated after env update. Env backup created on host (`.django.bak.ai21.*`).

## Isolation evidence

| Check | Result |
|-------|--------|
| Pilot `feature_enabled` | **True** |
| Non-pilot `feature_enabled` | **False** |
| Pilot bootstrap HTTP | **200** `enabled=true` |
| Non-pilot bootstrap HTTP | **200** `enabled=false` |
| Unauthenticated bootstrap | **401** |
| Non-pilot create conversation | **503** `research_copilot_disabled` |
| Pilot create conversation | **201** |
| Pilot send message HTTP | **200** |

## Live query evidence (pilot)

| Query | Tools / path | Result |
|-------|--------------|--------|
| What is my next booking? | `get_next_booking` ok + Ollama | **PASS** — portal-grounded “no upcoming booking” |
| How much does 5 XRD samples cost? | `search_equipment` + `estimate_booking_cost` ok | **PARTIAL** — tools PASS; LLM hit 60s timeout → controlled unavailable (sources still attached) |
| What is XRD? | Ollama | **PARTIAL** — timeout → controlled unavailable |
| What software can I use for PXRD? (HTTP) | conversation API | **PARTIAL** — HTTP 200; timeout unavailable text + PXRD sources |
| Cross-user results | foreign deny | **PASS** (`booking_not_found`) |
| Cancel own booking prepare | `requires_confirmation=true` | **PASS** (no silent cancel) |

## Core platform during pilot traffic

| Probe | Result |
|-------|--------|
| `/api/version` | 200 ~26 ms |
| analysis ready | 200 ~63 ms |
| Celery / Django | healthy; Ollama ~1.6 GiB / ~2 CPU during load |

## Verdict update

### **LIMITED PILOT ACTIVE (test account only)**

- Not global.
- Allowlist size: **1** (`test.student@iic-booking.test`).
- Known issue: intermittent **Ollama 60s timeouts** under sequential pilot queries → safe unavailable responses (core unaffected).

### Rollback

```bash
RESEARCH_COPILOT_ENABLED=false
# recreate django
```

Or restore `.envs/.production/.django.bak.ai21.*`.
