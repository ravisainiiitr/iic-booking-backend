# AI.25.3 — Production Smoke

**Deployed commit:** `3a72438` (`v2.5.41-ai25.3-copilot-deterministic`)

## Health

| Probe | Result |
|-------|--------|
| `/api/version/` | **200** |
| `/api/v1/analysis/health/ready/` | **200** |
| `/api/v1/analysis/health/live/` | **200** |
| `/api/equipments/` | **200** |
| Frontend `/` | **200** |
| Celery ping | **pong** |
| Redis | up (ready probe cache=ok historically) |

## Authenticated pilot

| Check | Result |
|-------|--------|
| Pilot bootstrap | **200** `enabled=true`, `access_mode=authenticated` |
| Non-pilot conversation create | **503** |
| Anonymous conversation create | **503** (Public OFF) |

## Configuration end-state

| Flag | Value |
|------|-------|
| ENABLED | true |
| PUBLIC | **false** |
| Pilot | `test.student@iic-booking.test` |
| MAX_CONCURRENT | 1 |
| MAX_TOKENS | 160 |
| OLLAMA_MODEL | llama3.2:1b |
| EquipmentPI | **0** (NOT CONFIGURED — unchanged) |

## Ollama failure drill

Not re-run in AI.25.3 (operational risk). Inherited AI.25.1 evidence: controlled unavailable message; booking/Celery/frontend healthy; recovery after restart without DB repair.

## Resources during smoke

Ollama idle after deterministic suite; Django briefly elevated during one Ollama explanation (~53s); Celery/Redis/frontend stable.
