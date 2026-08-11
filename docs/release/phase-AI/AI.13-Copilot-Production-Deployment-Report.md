# AI.13 — Research Copilot Production Deployment Report

**Date:** 2026-08-11  
**Mode:** AUTO MODE  
**Baseline:** Backend `95cdcb4` · Frontend `86cb60d` · Android `233740a` (unchanged)

## Final decision

**COPILOT READY FOR LIMITED PRODUCTION PILOT**

Production remains:

```
RESEARCH_COPILOT_ENABLED=false
```

Do **not** flip the flag globally until ops completes the remaining enablement checklist below (OpenAI key in prod secrets, migrations on prod, controlled live E2E with real pilot accounts).

**Not** selected: GLOBAL PRODUCTION (global-only flag; live E2E not executed with inventable credentials forbidden).

---

## 1. Current architecture

```
User → Research Copilot UI (Vite soft-gate + bootstrap enabled)
  → /api/v1/research-copilot/ (IsAuthenticated + feature gate + Copilot throttles)
  → Conversation (request.user scoped)
  → RAG / tools (permission-filtered)
  → LLM gateway (OpenAI + timeout) OR FallbackGateway
  → Response + suggested_actions (href cards; mutating → requires_confirmation)
  → User confirms in existing portal flows
  → Existing booking / wallet / RA / ticket services
  → CopilotAuditEvent
```

LLM never mutates bookings, wallet, results, equipment, users, or RA reservations directly.

## 2. Assessment

See [AI.13-Copilot-Assessment.md](./AI.13-Copilot-Assessment.md).

Pre-fix gaps closed in this phase:

| Gap | Fix |
|-----|-----|
| Copilot rate limits | `ResearchCopilotUserThrottle` / `ResearchCopilotToolThrottle` |
| LLM timeouts / max tokens | `RESEARCH_COPILOT_LLM_TIMEOUT_SECONDS`, `RESEARCH_COPILOT_MAX_TOKENS` |
| Cost / length guards | `RESEARCH_COPILOT_MAX_USER_MESSAGES`, `RESEARCH_COPILOT_MAX_INPUT_CHARS` |
| Knowledge admin flag | `_feature_gate()` on all knowledge admin + search routes |
| FEATURE_DISABLED audit | Written on gated API calls |
| Prompt injection | Hard rules + `<<<UNTRUSTED_DOCUMENT_CONTEXT>>>` wrapper |
| Frontend bootstrap gate | FAB/UI hidden when backend `enabled=false` |
| Confirmation UX | `requires_confirmation` badges / copy on action chips |

## 3. Security audit

| Check | Result |
|-------|--------|
| All Copilot endpoints authenticated | PASS |
| Feature flag enforced on backend | PASS (503 when OFF; bootstrap returns `enabled:false`) |
| Conversation ownership (`user=request.user`) | PASS (+ isolation test) |
| Tool foreign selectors denied | PASS (bookings/wallet) |
| Mutating tools = confirmation cards only | PASS |
| Knowledge RAG security levels + department | PASS (reuse existing) |
| Secrets not logged / not in responses | PASS (by design; keys env-only) |
| Core portal independent of Copilot | PASS (flag OFF; LLM failure → fallback / escalate) |

## 4. Tool inventory

| Tool | R/W | Confirmation | Notes |
|------|-----|--------------|-------|
| search_equipment | R | No | Portal data |
| search_slots | R | No | Portal data |
| search_bookings | R | No | Caller only |
| get_wallet | R | No | Caller only; foreign selectors forbidden |
| search_documentation | R | No | RAG ACL |
| recommend_software | R | No | Software-centric (no Equipment→PC) |
| create_booking | W* | Yes → portal | Card only |
| cancel_booking | W* | Yes → portal | Own booking |
| launch_remote_analysis | W* | Yes → portal | Own booking |
| create_support_ticket | W* | Yes → portal | Card only |

\*No silent mutation.

## 5–7. Authorization / conversation / knowledge

- Conversations: create/list/get/message/feedback scoped to owner; cross-user → 404.
- Knowledge admin: admin role + feature flag.
- Knowledge search: authenticated + feature flag + RAG permission filter.
- Private docs: existing `security_level` + department filters reused (no second ACL).

## 8. LLM configuration

| Item | Value |
|------|-------|
| Provider | OpenAI (existing) when `OPENAI_API_KEY` set |
| Fallback | `FallbackGateway` (deterministic) when key missing / errors |
| Model | `RESEARCH_COPILOT_MODEL` / existing OpenAI chat model settings |
| Timeout | `RESEARCH_COPILOT_LLM_TIMEOUT_SECONDS` (default 30) |
| Max tokens | `RESEARCH_COPILOT_MAX_TOKENS` (default 800) |
| Credentials | Environment only — never committed |

## 9. Failure handling

LLM timeout / provider / malformed → gateway returns `None` → safe escalate-style reply.  
Tool failure → `tool_failed` / audit.  
Feature OFF → 503 / bootstrap `enabled:false`.  
**Booking portal does not depend on Copilot.**

## 10–11. Rate limiting & cost controls

| Control | Default |
|---------|---------|
| `research_copilot_user` | 60/hour |
| `research_copilot_tool` | 30/hour |
| Max input chars | 4000 |
| Max user messages / conversation | 40 |
| Max output tokens | 800 |

Portal-wide throttling unchanged.

## 12. Tests

```
docker exec … pytest iic_booking/research_copilot/tests -q
→ 28 passed
```

Includes AI.1 / AI.2 / AI.3 + **AI.13 security suite**:

- Feature disabled audit
- Bootstrap `enabled=false`
- Conversation isolation
- Message length / conversation limit
- Prompt-injection prompt structure
- Mutating confirmation cards
- Knowledge admin gated when OFF

Evidence: `docs/release/phase-AI/ai13-pytest.log`

## 13. E2E

| Scenario | Status |
|----------|--------|
| Controlled pilot accounts (login → book/cancel via Copilot cards) | **BLOCKED** — no invent credentials / no authorized pilot accounts in this session |
| LLM unavailable → portal still books | Unit/fallback design PASS; live outage E2E not forced |

## 14. Deployment (recommended sequence)

1. Deploy backend + frontend **with flag still false**.
2. Run migrations including `research_copilot` on the target DB.
3. Health: `/api/version`, `/api/v1/provisioning/capabilities/` → `research_copilot=false`.
4. Configure `OPENAI_API_KEY` in prod secrets (never commit).
5. Controlled live E2E with named pilot users.
6. Only then set `RESEARCH_COPILOT_ENABLED=true` (global).

**Current production probe (2026-08-11):** `research_copilot=false` on `equip.iitr.ac.in` — correct.

## 15. Rollout

**Global flag only** today.

> Global flag only; controlled user/group/department rollout requires additional work (do not invent a second feature-flag framework).

Preferred ops sequence after qualification: keep OFF → enable briefly for admin/test → limited department communication → broader users — using process/comms because the flag is environment-global.

## 16. Monitoring

Reuse existing logs / audit table:

- `CopilotAuditEvent` (FEATURE_DISABLED, TOOL_EXECUTED, conversation/message, escalate)
- LLM warning logs (no secret payloads)
- DRF 429 on Copilot throttles

No new alert stack invented.

## 17. Remaining blockers before flipping ON

1. Production deploy of AI.13 commits (this work).
2. Prod migrations for `research_copilot` if not yet applied.
3. Valid `OPENAI_API_KEY` in production secrets (optional for fallback-only pilot; required for full LLM).
4. Controlled live E2E with authorized pilot accounts.
5. Explicit ops decision to set `RESEARCH_COPILOT_ENABLED=true`.
6. Optional: user-scoped rollout mechanism (future).

## 18. Final status matrix

| Area | PASS | PARTIAL | BLOCKED | Evidence |
|------|------|---------|---------|----------|
| Architecture | ✓ | | | Existing flow preserved |
| Authentication | ✓ | | | IsAuthenticated |
| Authorization | ✓ | | | Role + tool gates |
| User Isolation | ✓ | | | test_security_ai13 |
| Tool Security | ✓ | | | confirmation-only mutate |
| Confirmation | ✓ | | | requires_confirmation + portal href |
| Audit | ✓ | | | FEATURE_DISABLED + tools |
| Prompt Injection | ✓ | | | rules + wrapper + tests |
| Knowledge Security | ✓ | | | flag + RAG ACL |
| LLM Integration | ✓ | | | OpenAI + timeout + fallback |
| Error Handling | ✓ | | | fallback / escalate |
| Rate Limiting | ✓ | | | Copilot-scoped throttles |
| Cost Controls | ✓ | | | tokens / length / msg cap |
| Backend Tests | ✓ | | | 28 passed |
| Frontend | ✓ | | | build PASS; bootstrap gate |
| E2E | | | ✓ | No invent credentials |
| Production Deployment | | ✓ | | Code ready; flag still false on prod |
| Controlled Rollout | | ✓ | | Global flag only |
| Monitoring | ✓ | | | Audit + logs |

### Enablement gate checklist

- [x] Authentication  
- [x] Authorization  
- [x] User isolation  
- [x] Tool authorization  
- [x] Confirmation actions  
- [x] Audit logging  
- [x] Prompt injection protection  
- [x] LLM error handling  
- [x] Rate limiting  
- [x] Cost controls  
- [x] Backend tests  
- [x] Frontend build  
- [ ] Controlled E2E (accounts)  
- [ ] Production deployment of AI.13  
- [x] Health checks (prod still OFF)  

**Therefore:** do **not** enable Copilot in production in this phase.

---

## SHAs

| Repo | SHA |
|------|-----|
| Backend | `2fe7d12` |
| Frontend | `31121ba` |
| Android | `233740a` (unchanged) |

Production enablement: **not performed** (`research_copilot=false` on live probe).
