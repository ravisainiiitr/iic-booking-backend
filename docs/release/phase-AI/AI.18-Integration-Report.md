# AI.18 — Integration & Pilot Report

**Date:** 2026-08-11  
**Verdict (interim):** see final section after deploy gates

## Integration approach

Surgical merge onto `origin/master` (`71ae396`) as `feature/ai18-production-integration`:

- Brought: `iic_booking/research_copilot/**`, phase-AI docs, Copilot workflows, compose/settings/router wiring
- **Not** brought: conflicting equipment/remote_analysis R9/R11 history from PR #63 (preserves catalog SPA fixes)

PR #63 remains CONFLICTING and is **not** force-merged.

## Tests

| Suite | Result |
|-------|--------|
| AI.17 tip (prior) | 54 passed |
| Provider unit tests on AI.18 branch | **12 passed** |
| Full DB suite on AI.18 + fresh Postgres | **BLOCKED** — pre-existing `remote_anal_status_*` DuplicateTable during migrate on fresh DB (master RA migration history; not introduced by Copilot files) |
| Frontend npm build | Prior AI.17 PASS; re-run as env allows |
| Android gradle | NOT TESTED |

## Production gates (must stay OFF)

`RESEARCH_COPILOT_ENABLED=false` until: app installed, migrations applied, EC2 resources qualified, Ollama private+healthy, allowlist with **real** authorized emails, isolation evidence.
