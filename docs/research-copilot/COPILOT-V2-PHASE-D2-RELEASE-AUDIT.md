# Copilot V2 Phase D.2 — Release Audit

**Date:** 2026-08-26  
**Purpose:** Identify Phase D/D.1 source of truth and exclude unrelated dirty tree / hot-sync residue.

## Backend working tree (pre-commit)

| Category | Disposition |
|----------|-------------|
| Phase D/D.1 Copilot sources | **INCLUDE** in release |
| Phase D/D.1 docs + corpus + evidence | **INCLUDE** |
| `config/settings/base.py` Phase D flags only | **INCLUDE** (4-line additive diff) |
| Portal migration / Phase 10* docs & code | **EXCLUDE** (unrelated; leave dirty) |
| Equipment/users migration-related modified files | **EXCLUDE** |
| `tmp_probe_ra70.py` / `tmp_reset_ra*.py` | **EXCLUDE** (temp) |
| Workflow / deploy script edits | **EXCLUDE** |

### Phase D/D.1 file set (release)

```
config/settings/base.py                                          # COPILOT_ANALYSIS_ACTIONS, TICKET_CREATE, MULTI_INTENT
iic_booking/research_copilot/services/conversation.py
iic_booking/research_copilot/services/v2/intent_resolver.py
iic_booking/research_copilot/services/v2/orchestrator.py
iic_booking/research_copilot/services/v2/read_tools.py
iic_booking/research_copilot/services/v2/capability_map.py
iic_booking/research_copilot/services/v2/multi_intent.py
iic_booking/research_copilot/services/v2/unanswered.py
iic_booking/research_copilot/management/commands/copilot_phase_d1_controlled_e2e.py
iic_booking/research_copilot/tests/test_copilot_v2_phase_d.py
docs/research-copilot/COPILOT-V2-* (Phase D/D.1 + corpus + status)
```

### Hot-sync residue (must be replaced by clean image)

During D.1, files were `docker cp`'d into `iic-booking-backend-django-1`.  
D.2 requires: `git checkout <tag>` + `docker compose build` + `up -d` with **no** docker cp.

## Frontend working tree

| File | Disposition |
|------|-------------|
| `src/components/ResearchCopilot/index.tsx` | **INCLUDE** (comparison/list/dashboard cards) |
| Other modified pages (Wallet, Profile, Dashboard, …) | **EXCLUDE** (unrelated) |
| `.envs/.staging/`, `docker-compose.staging.yml` | **EXCLUDE** |

## Migration safety (production check)

```
research_copilot: 0001, 0002 applied
migrate --plan: No planned migration operations
```

**No production migrate required for Phase D.2.**

## Baseline tag

Production host before D.2: `v2.5.44-copilot-v2-phase-c` @ `498f87ea`

## Release tag (target)

`v2.5.45-copilot-v2-phase-d2` (backend)  
Frontend tag/commit to be recorded after FE commit.
