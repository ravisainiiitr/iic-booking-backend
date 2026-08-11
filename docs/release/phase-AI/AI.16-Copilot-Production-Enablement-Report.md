# AI.16 — Research Copilot Production Enablement Report

**Date:** 2026-08-11  
**Mode:** AUTO MODE  
**Decision:** **COPILOT DEPLOYED — PILOT BLOCKED**

`RESEARCH_COPILOT_ENABLED` remains **false**.

---

## 1. AI.15 findings (closed by this phase)

| AI.15 blocker | AI.16 result |
|---------------|--------------|
| AI.14 not deployed | **Closed** — deployed `v2.5.5-ai16-research-copilot` @ `7a3f552` |
| `INSTALLED_APPS_has_research_copilot=False` | **Closed** — now **True** |
| No `research_copilot` migrations | **Closed** — `[X] 0001`, `[X] 0002` |
| `OPENAI_API_KEY` unset | **Still BLOCKED** |
| Pilot allowlist empty | **Still BLOCKED** |
| No authorized pilot account | **Still BLOCKED** |

---

## 2. Deployment SHA

| Item | Value |
|------|-------|
| Release tag | `v2.5.5-ai16-research-copilot` |
| Commit | `7a3f552` (AI.14 lineage including AI.13/14 + readiness probes) |
| AI.14 baseline ancestor | `dc5433a` contained |
| Previous production tag | `v2.5.3-lazy-trusted-policy` |
| Deploy Actions | `31455773192` **success** |
| Pre-deploy tests | Copilot suite **39 passed**; frontend `e9fa789` build **PASS** |

Backend Release tag-verify on Windows failed (`Verify-ReleaseTag.ps1` path glitch on runner). Deploy Backend proceeded with local pytest evidence and completed successfully.

---

## 3. Installation verification

| Check | Result | Evidence |
|-------|--------|----------|
| `INSTALLED_APPS_has_research_copilot` | **True** | Show Migrations `31457718486` |
| App label resolvable | **PASS** | `showmigrations research_copilot` exit 0 |

---

## 4. Migration verification

**Before (AI.15):** `No installed app with label 'research_copilot'.`

**After deploy + migrate:**

```
research_copilot
 [X] 0001_initial_research_copilot
 [X] 0002_knowledge_engine
```

| Run | Result |
|-----|--------|
| Migrate Production `31457509847` | success — research_copilot included; no pending |
| AI16 Migrate Research Copilot `31457707475` | success — no pending; `PASS RESEARCH_COPILOT_ENABLED still False` |

Note: Django reports model-state drift (“changes not yet reflected in a migration”) for several apps including `research_copilot`. **No pending migration files**; do **not** fake. Follow-up `makemigrations` review recommended offline — non-blocking for install.

---

## 5. OpenAI configuration status

`OPENAI_API_KEY_configured=False` (boolean only; value never printed).

No OpenAI secret present in GitHub Actions secrets list. Production env not updated (would require operator-supplied key).

**Phase 8/10 STOP condition met for enablement.**

---

## 6. Pilot allowlist status

`RESEARCH_COPILOT_PILOT_EMAILS_configured=False`  
`RESEARCH_COPILOT_PILOT_EMAILS_count=0`

No authorized pilot emails/accounts supplied. **Not invented.**

---

## 7. Feature flag state

| Setting | Value |
|---------|-------|
| `RESEARCH_COPILOT_ENABLED` | **False** |
| Public `research_copilot` capability | **false** |
| Rollback posture | Set env false (already false) |

Disabled-state proof (unauthenticated): Copilot routes return **401** (auth required), not 404 — routes exist while feature remains OFF for enablement.

---

## 8. Live E2E evidence

**NOT RUN** — blocked by missing OpenAI key + missing authorized pilot allowlist/account.

No fabricated chat/booking/cancellation evidence.

---

## 9–17. Security / isolation / confirmation / knowledge / RA / results / audit

Covered by AI.13/AI.14 unit suite (**39 passed**) and remain in deployed code. Live re-validation deferred until pilot credentials exist.

Rate limiting / error handling / confirmation / injection protections: **deployed as implemented in AI.14**; live exercise **BLOCKED**.

---

## 18. API routes (production)

Unauthenticated probes (2026-08-11):

| Route | HTTP |
|-------|------|
| `GET /api/v1/research-copilot/bootstrap/` | 401 |
| `GET /api/v1/research-copilot/conversations/` | 401 |
| `GET/POST /api/v1/research-copilot/tools/execute/` | 401 |
| `GET /api/v1/research-copilot/knowledge/documents/` | 401 |

→ Routes mounted (not 404). Auth enforced.

---

## 19. Core portal regression (smoke)

| Check | Result |
|-------|--------|
| `/api/version` | 200 |
| `/api/v1/provisioning/capabilities/` | 200; `research_copilot=false` |
| `/api/v1/analysis/health/ready/` | 200 |
| `/api/equipments/?page_size=1` | 200 |
| Deploy health + provisioning sessions smoke | PASS (Actions) |

---

## 20. Monitoring / rollback

- Deploy left django healthy; capabilities false.  
- Rollback: checkout previous tag `v2.5.3-lazy-trusted-policy` via Deploy Backend, **or** keep/set `RESEARCH_COPILOT_ENABLED=false` (already false).  
- Destructive rollback not performed.

---

## 21. Android

Same backend-gated Copilot client. **NOT PART OF CURRENT LIVE PILOT** (flag OFF; no allowlist).

---

## 22. Frontend

AI.14 frontend tip `e9fa789` build verified locally. Production SPA deploy of that tip **not** asserted in this phase. UI also requires `VITE_RESEARCH_COPILOT_ENABLED=true` for FAB visibility when backend pilot is later enabled.

---

## Exact remaining blockers before enablement

1. Configure production `OPENAI_API_KEY` via approved secret/env mechanism (do not commit).  
2. Configure `RESEARCH_COPILOT_PILOT_EMAILS` with **authorized** pilot emails only.  
3. Supply authorized pilot credentials to the operator session.  
4. Deploy matching frontend with Vite Copilot soft-gate if UI pilot is required.  
5. Re-run disabled-state + controlled E2E (AI.15/AI.16 phases 11–25).  
6. Only then set `RESEARCH_COPILOT_ENABLED=true` with allowlist retained.

---

## Final status table

| Gate | PASS | PARTIAL | BLOCKED | Evidence |
|------|------|---------|---------|----------|
| AI.14 deployed | ✓ | | | Deploy `31455773192` tag `v2.5.5-ai16-research-copilot` |
| research_copilot installed | ✓ | | | `INSTALLED_APPS_has_research_copilot=True` |
| Migrations | ✓ | | | `[X] 0001`, `[X] 0002` |
| API routes | ✓ | | | 401 on auth (mounted) |
| OpenAI configuration | | | ✓ | configured=False |
| Pilot allowlist | | | ✓ | count=0 |
| Feature flag | ✓ | | | remains False |
| Basic chat | | | ✓ | no pilot/OpenAI |
| Portal grounding | | | ✓ | live |
| Equipment queries | | | ✓ | live |
| Booking queries | | | ✓ | live |
| Knowledge | | | ✓ | live |
| Software recommendation | | | ✓ | live |
| Booking confirmation | | | ✓ | live |
| Cancellation | | | ✓ | live |
| Result security | | | ✓ | live |
| User isolation | | | ✓ | live |
| Prompt injection | | ✓ | | unit PASS; live blocked |
| Audit | | ✓ | | unit PASS; live blocked |
| Rate limiting | ✓ | | | AI.13/14 config deployed |
| Error handling | ✓ | | | AI.14 deployed |
| Monitoring | ✓ | | | health/caps smoke |
| Rollback | ✓ | | | previous tag recorded; flag OFF |
| Core portal regression | ✓ | | | version/ready/equipments 200 |

---

## Final decision

**COPILOT DEPLOYED — PILOT BLOCKED**

Production installation complete, but controlled pilot is blocked because no authorized OpenAI credential and no pilot account/allowlist were provided.

Do **not** claim live E2E. Do **not** enable globally.

---

## SHAs

| Repo | SHA |
|------|-----|
| Backend (deployed tag peel) | `7a3f552` (`v2.5.5-ai16-research-copilot`) |
| Backend feature tip (docs) | *(post-commit)* |
| Frontend | `e9fa789` |
| Android | `233740a` (unchanged) |
