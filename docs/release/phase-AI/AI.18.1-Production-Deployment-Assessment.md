# AI.18.1 — Production Deployment Assessment

**Date:** 2026-08-11  
**Mode:** AUTO — clean flag-OFF deploy + root-cause of `v2.5.20` failure  
**Constraint:** Preserve R11 catalog lineage; `RESEARCH_COPILOT_ENABLED=false`; no Ollama until qualified; no pilot.

## Root cause of `v2.5.20-ai18-research-copilot-off` failure

| Item | Evidence |
|------|----------|
| Tag / commit | `v2.5.20-ai18-research-copilot-off` → `3b6d76a` (Merge PR #64) |
| Symptom | Deploy Backend `31517901833` / `31517322662`: Django **crash-loop** (`Up N seconds (health: starting)` cycling), then `curl: Connection refused` on `:8080`, rollback |
| Failure message | `DEPLOY FAILED: health check failed for http://127.0.0.1:8080/api/v1/analysis/health/ready/` |
| **Root cause** | `iic_booking/remote_analysis/catalog_admin_views.py` contained an **unmatched `}`** (`)        }` at ~line 777). AST parse of the AI.18 tag file raises `SyntaxError: unmatched '}'`. Import of the RA catalog module aborts Django boot before gunicorn binds. |
| Fix commit (not in v2.5.20) | `a662ac7` — `fix(RA): repair catalog_admin_views syntax error` (1-line deletion). **Not** an ancestor of `v2.5.20`. |
| Secondary factor | Concurrent R11 catalog deploys during the health-wait window (`31518498553` cancelled, `31518575116` success) increased operational noise; **primary boot failure preceded** those runs. |

**Conclusion:** AI.18 Copilot integration itself was not the crash trigger. The release tag was cut **before** the R11 catalog syntax fix landed on master.

## Pre-AI.18.1 live production state

| Field | Value |
|-------|-------|
| `current_release_tag` | `v2.5.5-r11-catalog-sync.2` |
| Deploy HEAD | `a662ac7` |
| `previous_release_tag` | `v2.5.20-ai18-research-copilot-off` |
| Portal ready / version | HTTP **200** / **200** (AI11 `31519847209`) |
| `research_copilot` on live image | **Present** (`.2` is after AI.18 merge + catalog fix) |
| Migrations `0001` / `0002` | **`[X]` applied** (Show Production Migrations `31519851133`) |
| Flag | Expected **false** (no enablement in this phase) |
| Ollama | **Absent** |

Note: Live already carried Copilot **code** via the R11 `.2` tag; the failed `v2.5.20` name was the broken pre-fix tip.

## Lineage protection (R11 + AI.18)

| Check | Result |
|-------|--------|
| `3b6d76a` ancestor of `origin/master` | Yes |
| `a662ac7` ancestor of `origin/master` | Yes |
| Force-push | **Not used** |
| New clean tag | `v2.5.21-ai18.1-research-copilot-off` → `639fee2` (master tip incl. AI.18 docs PR #65) |

## AI.18.1 deploy result

| Step | Result |
|------|--------|
| Quiet window | No in-progress deploy before dispatch |
| Deploy Backend | **`31520272970` success** — `PASS health`, `PASS deployed v2.5.21-ai18.1-research-copilot-off at 639fee2` |
| Live pointer | `current_release_tag=v2.5.21-ai18.1-research-copilot-off`, previous=`v2.5.5-r11-catalog-sync.2` |
| Migrations | `0001`/`0002` already `[X]`; migrate no-op; `PASS RESEARCH_COPILOT_ENABLED still False` |
| Ollama | Not installed |
| Pilot | Not enabled |

## Backend Release note

Tag-push Backend Release verify on the Windows runner continues to fail (pre-existing path/`Verify-ReleaseTag.ps1` issue). Production rollout uses **Deploy Backend** on the Linux EC2 runner, same as prior R11 tags.
