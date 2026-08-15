# AI.25.1 — Production Candidate Deployment

**Date (UTC):** 2026-08-15  
**Operator task:** Deploy AI.24.1 safely with **Public Copilot OFF**, then qualify.

## Candidate content

| Component | Intended SHA | What was deployed |
|-----------|--------------|-------------------|
| Backend Copilot package | `b7f0fb3` (+ AI.25 doc/test follow-ups in working tree) | Selective sync of `iic_booking/research_copilot/**` into production Django image; **not** a full unrelated R11/R12/R14/PI branch deploy |
| Frontend Copilot UI | `60cceaf` | `ResearchCopilot` + API client synced; frontend image rebuilt with `VITE_RESEARCH_COPILOT_ENABLED=true` |
| Migration | `research_copilot.0003_public_copilot_access` | Applied (`access_mode`, `anonymous_session_key`, nullable `user`) |

Production git HEADs on EC2 remain merge tips of other tracks; Copilot files were verified present (`0003` migration, `AccessMode`, anonymous key handling in `api_views.py`).

## Production safety gate (pre/post)

| Flag | Required | Observed end-state |
|------|----------|--------------------|
| `RESEARCH_COPILOT_ENABLED` | existing pilot ON | `true` |
| `RESEARCH_COPILOT_PUBLIC_ENABLED` | **false** | **false** |
| `RESEARCH_COPILOT_PILOT_EMAILS` | `test.student@iic-booking.test` only | unchanged |
| `RESEARCH_COPILOT_MAX_CONCURRENT` | `1` | `1` |
| `RESEARCH_COPILOT_MAX_TOKENS` | `160` | `160` |
| Ollama model | `llama3.2:1b` | `llama3.2:1b` (2 CPU / 8 GB envelope) |

### Compose env-file gotcha (fixed during deploy)

`docker-compose.production.yml` interpolates:

```yaml
RESEARCH_COPILOT_ENABLED: ${RESEARCH_COPILOT_ENABLED:-false}
RESEARCH_COPILOT_PILOT_EMAILS: ${RESEARCH_COPILOT_PILOT_EMAILS:-}
```

A plain recreate **overrides** values from `.envs/.production/.django` unless compose is invoked with an explicit env-file.

**Stabilization file used:** `/home/ubuntu/iic-booking-backend/.env.copilot-ai251`

```bash
cd /home/ubuntu/iic-booking-backend
docker compose -f docker-compose.production.yml --env-file .env.copilot-ai251 \
  up -d --no-deps --force-recreate django
```

## Deployment run

| Step | Result |
|------|--------|
| Backend Copilot sync + Django rebuild/recreate | **PASS** |
| Frontend Copilot sync + image rebuild | **PASS** |
| `migrate research_copilot` (`0003`) | **PASS** |
| Unrelated R11/R12/R14/PI/DSA/RAA code intentionally not bulk-deployed | **PASS** |
| Public flag initially OFF | **PASS** |

Container recreate timestamps (UTC): Django ~`2026-08-15T03:55Z`, frontend ~`2026-08-15T03:57Z`.

## Immediate smoke

| Check | Result |
|-------|--------|
| `/api/version/` (Host `iicbooking.iitr.ac.in`) | **200** — portal/backend `2.5.2`, `research_copilot_version` `0.1.0` |
| `/api/v1/analysis/health/ready/` | **200** ready |
| `/api/v1/analysis/health/live/` | **200** ok |
| Celery inspect ping | **pong** |
| Redis (container up / used by ready probe) | **ok** via analysis readiness |
| Frontend `/` | **200** |
| `/api/equipments/` | **200** |
| Anonymous bootstrap `/api/v1/research-copilot/bootstrap/` | **200** with `enabled: false` (public OFF) |

## Authenticated pilot smoke

| Check | Result |
|-------|--------|
| Pilot bootstrap | `enabled: true`, `access_mode: authenticated` |
| Pilot conversation create | **201** |
| Non-pilot conversation create | **503** `research_copilot_disabled` |
| Anonymous conversation create (public OFF) | **503** `research_copilot_disabled` |
| Pilot “next booking” tool path | works (`get_next_booking`) |

## Rollback procedure (documented)

1. Keep/set `RESEARCH_COPILOT_PUBLIC_ENABLED=false` (already required end-state).
2. If Copilot must be withdrawn: `RESEARCH_COPILOT_ENABLED=false` via `.env.copilot-ai251` + django recreate.
3. Redeploy previous Django/frontend images if code rollback required.
4. Do **not** manually rewrite DB rows to “fix” Copilot; `0003` is additive/safe to leave.

## Not changed

- Ollama envelope (no 3B, no concurrency/token changes)
- Pilot allowlist (no expansion)
- DNS / RAA / DSA
- Production PI configuration (`EquipmentPI` count remained `0`)
