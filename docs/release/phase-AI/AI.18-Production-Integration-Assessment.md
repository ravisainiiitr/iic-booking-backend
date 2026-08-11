# AI.18 — Production Integration Assessment (read-only)

**Date:** 2026-08-11  
**Mode:** Audit before merge/deploy  
**Probes:** AI15 `31516364346`, AI16 `31516368237`, Show Migrations `31516372299`, AI11 `31516455870`

## Production pointer

| Field | Value |
|-------|-------|
| Deploy path | `/home/ubuntu/iic-booking-backend` |
| HEAD | `71ae396` |
| Release tag | `v2.5.19-ra-catalog-spa-fix` |
| Previous tag | `v2.5.18-ra-r11` |
| Django | `iic-booking-backend-django-1` healthy |
| Ready / version | HTTP 200 / 200; `research_copilot_version=0.0.0` |

## Copilot presence (pre-integration)

| Check | Result |
|-------|--------|
| App installed | **False** |
| Migrations | **No installed app** |
| Flag | **False** |
| OpenAI key | **False** |
| Pilot emails | **0** |
| Ollama | **Not observed** |

## Merge strategy

PR #63 is **CONFLICTING** with master (R11/catalog). AI.18 uses **surgical integration** onto `origin/master`: bring `research_copilot` + wiring + docs/workflows only; do **not** overwrite equipment/remote_analysis catalog fixes.

## Decision

Integrate on `feature/ai18-production-integration`, deploy **flag OFF**, migrate Copilot only, EC2-qualify before Ollama, keep enablement blocked until allowlist + isolation evidence.
