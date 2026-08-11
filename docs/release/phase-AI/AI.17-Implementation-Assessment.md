# AI.17 — Implementation Assessment (pre-completion)

**Date:** 2026-08-11  
**Mode:** Source-verified audit before completing Parts 2–47  
**Baseline branch tip:** `feature/ai-copilot-android` @ `f468e35` (prior Ollama provider work)  
**Master note:** `master` at audit time **does not** contain `iic_booking/research_copilot/` (app lives on the AI feature branch / tag `v2.5.5-ai16-research-copilot`). Production deploy path must not assume master already carries Copilot sources.

## Verdict (pre-implementation)

| Layer | Status |
|-------|--------|
| Research Copilot app (AI.1–AI.16) | **IMPLEMENTED** on feature branch |
| Ollama provider (prior AI.17) | **IMPLEMENTED** (`OllamaGateway`, default `COPILOT_LLM_PROVIDER=ollama`) |
| FakeInferenceProvider | **MISSING** (tests mock urllib / use Fallback) |
| `COPILOT_PROVIDER` env alias | **MISSING** (only `COPILOT_LLM_PROVIDER`) |
| Max concurrent generations / queue limit | **MISSING** |
| LLM outside DB transaction | **BROKEN / UNSAFE** (`send_message` is `@transaction.atomic` around Ollama) |
| Docker CPU/memory limits for Ollama | **PARTIAL** (profile exists; limits commented / unset) |
| Production compose Ollama env | **PARTIAL** (flag OFF; no `COPILOT_*` / `OLLAMA_*` passthrough) |
| Frontend Copilot UX | **IMPLEMENTED** (backend flag authoritative; Vite soft gate) |
| Android Copilot | **IMPLEMENTED** (same `/api/v1/research-copilot/`) |
| Part 44 docs set | **PARTIAL** (Ollama assessment/setup/report only) |
| Live Ollama E2E | **BLOCKED** previously; host has `ollama 0.32.9` but **no models pulled** at audit |
| Production EC2 Ollama | **NOT VERIFIED** this session (inspect before install) |
| Pilot enablement | **OFF** — keep `RESEARCH_COPILOT_ENABLED=false` |

## Already implemented (reuse — do not duplicate)

- Django app: models, migrations `0001`/`0002`, admin, URLs under `/api/v1/research-copilot/`
- Feature gates: `RESEARCH_COPILOT_ENABLED`, `RESEARCH_COPILOT_PILOT_EMAILS`
- Throttles: `60/hour` chat, `30/hour` tools (configurable)
- Portal grounding + allowlisted tools + confirmation mutations
- Knowledge engine + citations + Report Incorrect + admin knowledge UI
- Prompt-injection wrappers (`<<<UNTRUSTED_DOCUMENT_CONTEXT>>>`)
- Audit logging for conversations / tools / feedback
- `LLMGateway` ABC + `OllamaGateway` + optional `OpenAIGateway` + `FallbackGateway`
- Staff LLM health endpoint (no secrets / no base URL)
- Frontend `ResearchCopilot` + Android `CopilotScreen` / repository
- Production workflows: AI.15 readiness probe, AI.16 migrate (flag stays false)

## Partially implemented

| Item | Gap |
|------|-----|
| Provider config naming | Brief wants `COPILOT_PROVIDER`; code uses `COPILOT_LLM_PROVIDER` |
| Provider API surface | Brief wants `generate()` / `model availability()`; code has `complete()` / `health()` |
| Resource isolation | Timeouts/token/input limits exist; concurrency + Docker limits incomplete |
| Failure UX | Unavailable copy exists; dedicated “busy” overload message missing |
| Quick actions | Several portal prompts exist; next-booking / slots / cost prompts incomplete on FE |
| Docs | Need Architecture / Security / Performance / Test / Production-Deployment / Implementation Report |

## Missing / broken / unsafe

| Item | Classification |
|------|----------------|
| Inference held inside `@transaction.atomic` | **UNSAFE** — can pin DB connections during LLM latency |
| Unlimited concurrent Copilot generations | **MISSING** — risk to shared host |
| `FakeInferenceProvider` for deterministic unit tests | **MISSING** |
| Production Ollama service + CPU/RAM caps | **MISSING** (must inspect EC2 first) |
| Model pull on this host | **MISSING** — `ollama list` empty |
| Controlled live pilot | **BLOCKED** — flag OFF by design until isolation + model verified |

## Duplicate / redesign risks (explicitly avoided)

- No second Copilot engine
- No duplicate booking/wallet/RA APIs
- No mandatory `OPENAI_API_KEY` for production inference path
- No Celery move of booking into Copilot workers

## Requires deployment (after code complete)

1. Merge/ship Copilot sources to the production deploy pointer (not only feature branch)
2. Apply `research_copilot` migrations if not already applied (AI.16 workflow)
3. Configure `COPILOT_PROVIDER=ollama` + private `OLLAMA_BASE_URL` + `OLLAMA_MODEL`
4. Install/bound Ollama **separately** with CPU/memory limits after EC2 inspection
5. Keep `RESEARCH_COPILOT_ENABLED=false` until controlled pilot procedure
6. Configure `RESEARCH_COPILOT_PILOT_EMAILS` before any enablement

## Completion plan for this phase

1. Fix transaction isolation + add concurrency gate  
2. Add `FakeInferenceProvider`, `COPILOT_PROVIDER` alias, `generate()`  
3. Harden Docker/local resource limits; production env passthrough (still OFF)  
4. Frontend/Android quick-action + busy/unavailable polish  
5. Tests + docs (Part 44)  
6. Read-only production inspection; deploy only when safe with flag OFF  
