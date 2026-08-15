# AI.25.3 — Production Deployment

**Date (UTC):** 2026-08-15T08:03Z–08:09Z  
**Release tag:** `v2.5.41-ai25.3-copilot-deterministic`  
**Qualified commit:** `3a72438a033993b035bb36238c96e55abb55d9bd`  
**Previous production base:** `20321ff` / tag `v2.5.40-r13-ghost-reserved` (unchanged checkout tip)

## Why not full `Deploy Backend` tag checkout

The GitHub `Deploy Backend` workflow checks out the entire release tag and rebuilds all compose services. The AI.25.2 commit lives on `feature/r13-allocation-ux-data-first`, while production remains on the `v2.5.40` hotfix tip with AI.24.1 Copilot files already present.

A full checkout of `v2.5.41-ai25.3-copilot-deterministic` would risk replacing the production base with unrelated feature-branch content.

**Formal AI.25.3 method (existing compose build workflow, surgical content):**

1. Push committed AI.25.2 + annotated release tag to `origin`
2. On EC2: `git fetch` tag; `git checkout <commit> --` **only** AI.25.2 Copilot paths
3. `docker compose … build django` — **permanent image bake** (not `docker cp` into a running container)
4. Recreate Django with `--env-file .env.copilot-ai251` (`PUBLIC=false`)

This satisfies: committed source permanently in the Django image, no temporary runtime sync, no unrelated R11/R12/PI deploy.

## Configuration (verified)

```text
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PUBLIC_ENABLED=false
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
RESEARCH_COPILOT_MAX_CONCURRENT=1
RESEARCH_COPILOT_MAX_TOKENS=160
OLLAMA_MODEL=llama3.2:1b
```

## Migrations

**No new migration required** (AI.25.2 is routing/logic only; AI.24.1 `0003` already applied).

## Deploy evidence

| Item | Value |
|------|-------|
| Image | `iic_booking_production_django` rebuilt |
| Marker in image | `/app/iic_booking/research_copilot/AI253_DEPLOYED.txt` |
| `BACKEND_GIT_COMMIT` | `3a72438a033993b035bb36238c96e55abb55d9bd` |
| Host state files | `.deploy-state/ai253_git_ref`, `ai253_release_tag` |
| Backup of prior files | `.deploy-state/ai253-backup-<ts>/` |

## Rollback

1. Restore backed-up `portal_grounding.py` / `conversation.py` from `.deploy-state/ai253-backup-*`
2. Or `git checkout <prior> --` those paths from pre-AI.25.3 tree
3. `docker compose -f docker-compose.production.yml --env-file .env.copilot-ai251 build django`
4. `… up -d --no-deps --force-recreate django`
5. Keep `RESEARCH_COPILOT_PUBLIC_ENABLED=false`

Do not manually rewrite DB rows to recover Copilot.
