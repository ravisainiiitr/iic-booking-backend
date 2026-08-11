# AI.18.1 — Integration / Qualification Report

**Date:** 2026-08-11  
**Mode:** AUTO — clean production deploy (flag OFF) + EC2 resource qualification  
**Final verdict:** see bottom

## Executive summary

Prior tag `v2.5.20-ai18-research-copilot-off` failed because it shipped a **syntax-broken** `catalog_admin_views.py` (R11 catalog bug), not because Research Copilot itself crashed the process. Clean tag `v2.5.21-ai18.1-research-copilot-off` (`639fee2`) deployed successfully with R11 preserved, Copilot installed, migrations applied, flag **OFF**, and **no Ollama**. Co-resident Ollama on the production EC2 is **BLOCKED**.

## Root cause (v2.5.20)

| | |
|--|--|
| Tag | `v2.5.20-ai18-research-copilot-off` → `3b6d76a` |
| Failure | Deploy `31517901833`: Django crash-loop → connection refused on `:8080` → rollback |
| Cause | Unmatched `}` in `catalog_admin_views.py` (`SyntaxError`); fixed in `a662ac7` (not in v2.5.20) |
| Concurrent noise | R11 deploys during health wait; primary crash preceded them |

## Deploy outcome (AI.18.1)

| Field | Value |
|-------|-------|
| New tag | **`v2.5.21-ai18.1-research-copilot-off`** |
| Commit | **`639fee2`** |
| Deploy run | **`31520272970` success** |
| Live `current_release_tag` | `v2.5.21-ai18.1-research-copilot-off` |
| Live `previous_release_tag` | `v2.5.5-r11-catalog-sync.2` |
| R11 catalog lineage | Preserved (`a662ac7` ancestor of tag; no force-push; no R11 revert) |

## Migrations

| Step | Result |
|------|--------|
| AI16 Migrate Research Copilot `31520513861` | success |
| Before/after | `[X] 0001_initial_research_copilot`, `[X] 0002_knowledge_engine` |
| Migrate | `No migrations to apply` |
| Flag after migrate | **`PASS RESEARCH_COPILOT_ENABLED still False`** |

## Health regression

| Check | Result |
|-------|--------|
| Ready | HTTP **200** (AI11 `31520519490`) |
| Version | HTTP **200**; reports `research_copilot_version=0.1.0` |
| 5xx sample | No ERROR/Traceback spike in sampled django tail |

## Disabled-state security

| Check | Result |
|-------|--------|
| Flag false after migrate | **PASS** |
| Unauthenticated Copilot HTTP 401/403 | **PASS** — AI18.1 Disabled State Security Probe `31521079127` |
| Unit tests (`test_disabled_gate`, `test_feature_disabled_writes_audit`) | Local compose blocked by container name conflicts; production disabled probe used instead |
| Pilot / credentials invented | **No** |
| AI15 readiness | **PASS** `31521087822` — app installed, flag false, pilot count 0, migrations applied, capabilities `research_copilot=False`, ready 200 |


## EC2 / Ollama

See `AI.18.1-EC2-Resource-Qualification.md`.

| Decision | **BLOCKED** (co-resident Ollama on production EC2) |
|----------|-----------------------------------------------------|
| Install performed | **No** |
| Copilot enabled | **No** |

## Client builds

| Client | Result |
|--------|--------|
| Frontend `npm run build` | **PASS** (~23s) on `iic-booking-frontend` |
| Android `gradlew test` | **PASS** (BUILD SUCCESSFUL) on `feature/ai17-copilot-ux` checkout |

## DuplicateTable / test-DB classification

| Item | Classification |
|------|----------------|
| Symptom | Fresh Postgres migrate fails with **`DuplicateTable` / index conflict** on `remote_anal_status_*` (remote_analysis migration history) |
| Scope | **Pre-existing master RA migration hygiene issue** — blocks full `migrate` + full Copilot DB suite on virgin DBs |
| Introduced by AI.18 Copilot files? | **No** (observed on master RA lineage independent of Copilot app tree) |
| Production impact | **None observed** — production `showmigrations` healthy; Copilot 0001/0002 applied |
| Action | Track as separate RA migration repair; do not block flag-OFF Copilot code deploy |

## Test matrix

| ID | Test | Result |
|----|------|--------|
| T1 | Audit v2.5.20 failure | **PASS** (syntax root cause proven via AST) |
| T2 | AI.18 + R11 on master lineage | **PASS** |
| T3 | Quiet-window Deploy Backend v2.5.21 | **PASS** `31520272970` |
| T4 | research_copilot migrate 0001/0002 | **PASS** (already applied / no-op) |
| T5 | Flag remains false | **PASS** |
| T6 | Portal ready/version regression | **PASS** |
| T7 | EC2 resource probe | **PASS** — nproc=4, ~15.5GiB RAM, no GPU, no Ollama (`31521083962`) |
| T8 | Ollama suitability | **BLOCKED** co-resident |
| T9 | Ollama install / failure isolation | **SKIPPED** (blocked) |
| T10 | Copilot enable / pilot | **SKIPPED** (forbidden this phase) |
| T11 | FE npm build | **PASS** |
| T12 | Android gradle test | **PASS** |
| T13 | DuplicateTable classification | **PASS** (documented) |
| T14 | Disabled HTTP security probe | **PASS** `31521079127` |
| T15 | Backend Release Windows tag verify | **FAIL** (pre-existing; non-blocking for Linux Deploy Backend) |
| T16 | AI15 readiness | **PASS** `31521087822` |

## Blockers remaining

1. **Co-resident Ollama blocked** on production EC2 (4 vCPU, no swap, disk 72%, Guacamole footprint, CPU contention).  
2. **Windows Backend Release verify** still broken (process debt).  
3. **Virgin-DB DuplicateTable** blocks full local migrate suites.  
4. Live prod tip is still `v2.5.21` (`639fee2`); docs/probe merge `530e452` is on master but not required for runtime.


## Next operator action

1. Merge AI.18.1 docs/probe PR if not already on master.  
2. Dispatch **AI18.1 Disabled State Security Probe** + refreshed **AI17 Host Resource Probe**.  
3. For AI.18.2: choose **dedicated AI host** (preferred) or operator-signed co-resident capacity plan — then private Ollama architecture with localhost bind + cgroup caps.  
4. **Do not** set `RESEARCH_COPILOT_ENABLED=true` or pilot emails until AI.18.2+ approval.

## Final verdict

**PARTIAL — BLOCKED**

Clean flag-OFF Copilot deploy succeeded and R11 is preserved, but Ollama private install is **not** approved on the current production EC2, so AI.18.2 cannot proceed as an install-on-this-host qualification without a capacity/hosting decision.
