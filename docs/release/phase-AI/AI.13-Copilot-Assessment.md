# AI.13 — Research Copilot Assessment

**Date:** 2026-08-11  
**Baseline backend:** `95cdcb4`  
**Mode:** Inventory before enablement (production remains OFF)

## Architecture (preserve)

```
User → Research Copilot UI → /api/v1/research-copilot/
  → Conversation (user-scoped)
  → Tool / Knowledge retrieval (permission-filtered)
  → LLM gateway OR deterministic fallback
  → Response + suggested_actions (href confirmation cards)
  → User confirms in portal → existing booking/RA services
  → Audit (CopilotAuditEvent)
```

LLM never mutates bookings/wallet/results directly. Mutating tools return **confirmation cards** only.

## Scorecard (post AI.13 hardening)

| Area | Status | Notes |
|------|--------|-------|
| App package / models / migrations | IMPLEMENTED | `iic_booking.research_copilot` |
| Feature flag backend | IMPLEMENTED | `RESEARCH_COPILOT_ENABLED` default false → 503; bootstrap `enabled:false` |
| Feature flag frontend | IMPLEMENTED | Vite + bootstrap `enabled` soft-hide |
| Authentication | IMPLEMENTED | All endpoints `IsAuthenticated` |
| User isolation (bookings/wallet/tools) | IMPLEMENTED | Foreign selectors denied |
| Conversation ownership | IMPLEMENTED | Filtered by `request.user` + isolation tests |
| Knowledge RAG permissions | IMPLEMENTED | Security levels + dept |
| Knowledge admin flag gate | IMPLEMENTED | All knowledge admin + search gated |
| Mutating confirmation pattern | IMPLEMENTED | Cards + portal href + `requires_confirmation` |
| Frontend confirmation UX | IMPLEMENTED | Review & confirm labeling |
| Audit TOOL_EXECUTED/DENIED / FEATURE_DISABLED | IMPLEMENTED | |
| Prompt injection defense | IMPLEMENTED | Rules + untrusted wrapper + tests |
| LLM integration | IMPLEMENTED | OpenAI + timeout + FallbackGateway |
| Rate limiting | IMPLEMENTED | Copilot-scoped DRF throttles |
| Cost controls | IMPLEMENTED | max tokens / input / conversation length |
| Tests | IMPLEMENTED | 28 passed (AI.1–AI.3 + AI.13 security) |
| Production enablement | OFF | capabilities `research_copilot=false` |

## Tools inventory

| Tool | R/W | Confirmation | Permission |
|------|-----|--------------|------------|
| search_equipment | R | No | Authenticated |
| search_slots | R | No | Authenticated |
| search_bookings | R | No | Own bookings only |
| get_wallet | R | No | Own wallet only |
| search_documentation | R | No | RAG security level |
| recommend_software | R | No | Authenticated |
| create_booking | W* | Yes → portal | Role-gated; no server mutate |
| cancel_booking | W* | Yes → portal | Own booking only |
| create_support_ticket | W* | Yes → portal | Card only |
| launch_remote_analysis | W* | Yes → portal | Own booking |

\*Write intent only via confirmation card; execution is existing portal APIs.

## Env configuration (names only)

| Variable | Required for pilot | Default | Sensitive |
|----------|-------------------|---------|-----------|
| `RESEARCH_COPILOT_ENABLED` | Yes | `false` | No |
| `OPENAI_API_KEY` | For live LLM | empty → FallbackGateway | **Yes** |
| `OPENAI_CHAT_MODEL` / `RESEARCH_COPILOT_MODEL` | Optional | gateway default | No |
| `RESEARCH_COPILOT_VERSION` | Optional | settings | No |
| Embedding/vector settings | Optional | local/auto | Partial |
| `VITE_RESEARCH_COPILOT_ENABLED` | Frontend build | unset → hidden | No |

## Critical gaps to close in AI.13 (before any enable)

1. Copilot-scoped rate limiting  
2. LLM client timeouts  
3. Feature-flag knowledge admin + audit FEATURE_DISABLED  
4. Frontend respect bootstrap `enabled=false`  
5. Prompt-injection hardening + tests  
6. Conversation cross-user isolation test  
7. Cost/conversation length guards  
8. Clearer confirmation action presentation (reuse suggested_actions)

## Explicit non-goals

- No second booking engine  
- No Equipment→Remote PC mapping  
- No FCM enablement  
- No inventing OpenAI keys or enabling production globally without gates  
