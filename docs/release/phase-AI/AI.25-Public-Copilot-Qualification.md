# AI.25 — Public Copilot Qualification

## Final decision

**PARTIAL — QUALIFICATION COMPLETE BUT OPERATIONAL ISSUE REMAINS**

### Meaning

- AI.24.1 **PostgreSQL security/functional tests PASS** (35 passed)
- Frontend production **build PASS**
- Production host health (Celery / Ollama envelope / flags) **observed healthy**
- **AI.23 full 86-query regression against AI.24.1 code was NOT executed** (deploy forbidden in AI.25; production still runs pre-AI.24.1 image)
- Therefore: **not yet approved** for controlled Public Copilot production enablement

**Do not enable `RESEARCH_COPILOT_PUBLIC_ENABLED` in production based on this report alone.**

---

## Scope respected

| Rule | Status |
|------|--------|
| No production deploy | **Honored** |
| No public flag enablement | **Honored** (`PUBLIC_ENABLED` unset on prod) |
| No pilot expansion | **Honored** (`test.student@iic-booking.test` only) |
| No Ollama envelope change | **Honored** (`1b`, concurrent 1, max_tokens 160, ~8GiB) |
| No second Copilot / provider | **Honored** |
| Android public mode | **NOT IN SCOPE** |

Commits under qualification:

- Backend AI.24.1: `b7f0fb3`
- Frontend AI.24.1: `60cceaf`
- AI.25 unblockers (this work): migration idempotency + test assertion fixes (see git)

---

## PostgreSQL evidence

Environment: Docker Compose `docker-compose.local.yml` + `docker-compose.test.yml`  
Image: `iic_booking_local_django`  
DB: `postgres:16-alpine` (`iic_booking_test`)

Command:

```bash
docker compose -f docker-compose.local.yml -f docker-compose.test.yml run --rm --no-deps \
  -e DJANGO_SETTINGS_MODULE=config.settings.test \
  -e COPILOT_PROVIDER=fake -e COPILOT_LLM_PROVIDER=fake \
  django pytest \
  iic_booking/research_copilot/tests/test_ai241_public_auth.py \
  iic_booking/research_copilot/tests/test_ai25_prehandler_acl.py \
  iic_booking/research_copilot/tests/test_security_ai13.py \
  iic_booking/research_copilot/tests/test_tools_ai3.py \
  -q --reuse-db
```

**Result: 35 passed** (log: `ai25-pytest-final.log`)

Includes:

- Public bootstrap / public Q / private Q → login
- Private tool rejection
- Pre-handler ACL (handlers never called when rejected)
- Anonymous key is **not** authorization
- Cross-user conversation isolation
- Tool ACL matrix
- AI.13 security + AI.3 tools

### Migration note

Fresh Postgres hit a pre-existing `remote_analysis.0017` duplicate-index bug during migrate/pytest create-db.  
Minimal idempotent fix: skip re-`add_index` when `create_model` just created `TunnelSession` (same schema_editor flush).  
This is a test/migrate unblocker, **not** a Copilot behavior change.

---

## Public mode matrix (automated)

| Item | Result |
|------|--------|
| Public questions | **PASS** (FakeInference + API) |
| Public equipment tool | **PASS** |
| Public tool list excludes private | **PASS** |
| Private question → login | **PASS** |
| Private tool rejection | **PASS** |
| Pre-handler rejection | **PASS** |
| Anonymous key required | **PASS** |
| Anonymous key ≠ auth | **PASS** |
| Anonymous throttling live load | **NOT RUN** (unit settings present; soak not executed) |

---

## Frontend

`npm run build` in `iic-booking-frontend` → **PASS** (~30.5s)  
Log: `ai25-frontend-build.log`  
Includes AI.24.1 public FAB / anon key / login CTA code (`60cceaf`).

---

## Production posture (observation only)

| Check | Result |
|-------|--------|
| Django container | healthy |
| Celery ping | **pong / 1 node online** |
| Ollama | up; mem cap ~8GiB; model `llama3.2:1b` |
| `RESEARCH_COPILOT_ENABLED` | `true` |
| Pilot emails | `test.student@iic-booking.test` |
| `MAX_CONCURRENT` / `MAX_TOKENS` | `1` / `160` |
| `RESEARCH_COPILOT_PUBLIC_ENABLED` | **unset** (public not production-enabled) |
| AI.24.1 code on prod | **NOT deployed** |
| Ollama stop/recovery drill | **NOT RUN** (would disrupt live pilot) |

Log: `ai25-ec2-health.log`

---

## Gate for controlled public enablement (next separate step)

1. Deploy AI.24.1 backend+frontend to staging/prod **with public flag still false**
2. Run **full AI.23 86-query** on authenticated pilot against that build
3. Run anonymous live matrix + throttle soak
4. Controlled Ollama failure drill
5. Only then operator-approve `RESEARCH_COPILOT_PUBLIC_ENABLED=true`

Until step 2 completes: **do not claim PASS — READY FOR CONTROLLED PUBLIC COPILOT ENABLEMENT**.
