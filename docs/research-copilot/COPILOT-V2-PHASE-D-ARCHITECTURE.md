# Copilot V2 — Phase D Architecture

**Status:** IMPLEMENTED (MVP orchestrator unification) — controlled pilot readiness assessed separately  
**Date:** 2026-03-26  
**Principle:** Copilot = orchestrator over authoritative portal services (not a second booking/wallet/RAA system).

## Vision

Unify Phase A (deterministic reads), Phase B (booking mutations), and Phase C (wallet/financial) into one **IIC Research Assistant** that understands user objectives and routes to live portal capabilities.

## Architecture

```
USER → NL → INTENT ENGINE (deterministic-first)
              ├── read_tools / capability_map / compare
              ├── multi_intent planner (capped)
              ├── RAG (approved knowledge)
              ├── mutations (flag-gated proposals)
              └── unanswered → KnowledgeGap queue
         → DOMAIN SERVICES (equipment, booking, wallet, RAA, tickets)
         → AUDIT / EVENTS
```

LLM is used only when deterministic paths return `None`. Read-only paths remain usable if LLM quota is exhausted.

## New / extended modules

| Module | Role |
|--------|------|
| `services/v2/capability_map.py` | Goal → technique → catalog needles |
| `services/v2/multi_intent.py` | Compound request decomposition (word-boundary safe) |
| `services/v2/unanswered.py` | Soft refusal + `KnowledgeGap` logging |
| `services/v2/read_tools.py` | `capability_search`, `compare_equipment`, `daily_dashboard`, `user_profile`, `support_ticket_assist` |
| `services/v2/intent_resolver.py` | New intent families |
| `services/v2/orchestrator.py` | Dispatch + multi-intent + ordinal context (“the second one”) |

## Intent families (deterministic subset)

INFORMATION / EQUIPMENT_SEARCH / CAPABILITY_SEARCH / EQUIPMENT_COMPARISON / SLOT_SEARCH / COST_ESTIMATE / BOOKING_* / WALLET_* / ANALYSIS / RA / USER_PROFILE / DAILY_DASHBOARD / SUPPORT / HELP / UNKNOWN

## Feature flags (Phase D additions)

| Flag | Default | Notes |
|------|---------|-------|
| `COPILOT_MULTI_INTENT` | `true` | Compound read orchestration |
| `COPILOT_ANALYSIS_ACTIONS` | `false` | Mutating analysis actions (not auto-enabled) |
| `COPILOT_TICKET_CREATE` | `false` | Ticket creation via Copilot (deep-link only when off) |

Preserved OFF: `COPILOT_BOOKING_*`, `COPILOT_WALLET_RECHARGE`, `COPILOT_WALLET_CREDIT`, `COPILOT_FINANCIAL_PROPOSALS`.

## Conversational memory

Cache key `copilot_ctx:{conversation.id}` stores:

- `last_equipment_id`, `slot_id`, proposal tokens
- `equipment_choices` for ordinal follow-ups (“second one”)

## Explicit non-goals (this phase)

- No second booking engine
- No auto ticket creation without flag + confirmation
- No Channel-I / T0 / RAA domain rewrites
- No inventing slots, balances, or equipment not in portal

## Frontend

- Equipment list + comparison cards
- Daily dashboard / profile card stubs
- Expanded bootstrap quick actions (My day, Compare XRD, Capability, Results, Support, Profile)

## Services reused (not rewritten)

Equipment catalog search, slot availability, booking prepare/execute, wallet read/prepare, RAG retrieve, RAA status tools, KnowledgeGap admin queue.
