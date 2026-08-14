# AI.21 — Pilot Observation Report

**Status:** **LIMITED PILOT ACTIVE** (seeded test student only)

**Allowlist:** `test.student@iic-booking.test`  
**Flag:** `RESEARCH_COPILOT_ENABLED=true`

## Observation log

| Timestamp (UTC) | Event | Notes |
|-----------------|-------|-------|
| 2026-08-14 19:45 | Preflight | Ollama healthy; flag OFF; allowlist count 0 |
| 2026-08-14 19:47 | Durable Django rebuild | AI.20 Copilot files baked into production image |
| 2026-08-14 19:48 | Post-deploy verify | bootstrap disabled; gate 503 |
| 2026-08-14 19:57 | Operator authorized seed test email | allowlist + enable |
| 2026-08-14 19:57–20:01 | Live pilot matrix | next-booking PASS; some LLM timeouts on pricing/general/HTTP software |
| 2026-08-14 20:01 | Core health | version/ready OK; Ollama ~2 CPU / 1.6 GiB |

## Query quality (initial)

| Class | Outcome |
|-------|---------|
| Booking (next) | correct portal-grounded empty result |
| Pricing tools | correct chain; LLM timeout on narrative |
| General science | timeout / unavailable (safe) |
| Software HTTP | unavailable text + citations (safe) |
| Authz cross-user | correctly refused |
| Mutation prepare | confirmation required |

Do not claim high % accuracy yet — sample size small; timeouts frequent under sequential load.
