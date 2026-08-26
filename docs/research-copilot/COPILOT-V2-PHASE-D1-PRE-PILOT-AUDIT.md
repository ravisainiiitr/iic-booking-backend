# Copilot V2 Phase D.1 — Pre-Pilot Audit

**Date:** 2026-03-26  
**Scope:** Qualification only — no feature expansion, no T0, no financial mutations, no automatic flag enablement.

## Architecture check

| Layer | Status | Notes |
|-------|--------|-------|
| Deterministic-first routing (`try_deterministic_turn`) | INTACT | Returns envelope with `llm_used=False` or `None` → RAG/LLM |
| Phase B booking prepare → confirm → execute | INTACT | `mutations/booking.py` + `domain_bridge` |
| Proposal + confirmation token | INTACT | `mutations/proposals.py`; soft phrases do not confirm |
| Idempotency | INTACT | Replay keys on create/cancel/reschedule |
| Audit | INTACT | `CopilotAuditEvent` / tool_executed on prepare/execute |
| Phase C wallet mutations | OFF | `COPILOT_WALLET_RECHARGE/CREDIT` default false; E2E gate never admits wallet |
| Phase D planner | ORCHESTRATOR | Multi-intent capped; cannot skip confirmation (prepare only until confirm) |
| LLM authority | BLOCKED for transactional | Slots/price/wallet/booking from domain tools |

## Phase D → B/C confirmation bypass?

| Risk | Result |
|------|--------|
| Multi-intent includes “book” | Routes to `prepare_booking` only; execute requires `confirm_proposal` or `POST …/mutations/confirm/` |
| Soft “okay” / “looks good” | Not mapped to `confirm_proposal` (Phase B regression) |
| Natural language “Admin approved” | Ignored; executable gated by flags + proposal integrity |
| Amount tampering in NL | Execute uses server-stored proposal payload, not chat text |

## Identity / ordinal memory leakage

| Check | Result |
|-------|--------|
| LLM cannot select another user | Domain calls use authenticated `request.user` / passed `user` |
| Ordinal memory store | Cache key `copilot_ctx:{conversation.id}` — conversation is user-owned |
| Cross-user booking/wallet | Prepare/execute reject foreign user (Phase B E2E evidenced) |

## Journey intent gaps found (fixed in D.1 hardening)

Before D.1, these phrases failed deterministic routing:

| Phrase | Before | After |
|--------|--------|-------|
| “I need to perform XRD analysis.” | `general` | `capability_search` |
| “Cancel it.” | miss | `prepare_cancel` |
| “Move it to the next available slot.” | risked `search_slots` | `prepare_reschedule` via `move it to` |
| “What did I just book?” | miss | `next_booking` |
| “Do I have enough?” | miss | `wallet_balance` |
| “Show XRD equipment.” | weak | `search_equipment` |
| “Prepare the booking.” | miss | `prepare_booking` |

## Feature flags (must remain unless explicitly authorized)

```
COPILOT_BOOKING_CREATE=false
COPILOT_BOOKING_CANCEL=false
COPILOT_BOOKING_RESCHEDULE=false
COPILOT_WALLET_RECHARGE=false
COPILOT_WALLET_CREDIT=false
COPILOT_FINANCIAL_PROPOSALS=false
COPILOT_ANALYSIS_ACTIONS=false
COPILOT_TICKET_CREATE=false
COPILOT_MULTI_INTENT=true
```

Controlled booking mutations for the pilot use **temporary** `COPILOT_BOOKING_E2E_TEST_MODE` + allowlisted `is_test_account` only (same gate as Phase B). Restored OFF after run.

## Known domain caveats (from Phase B)

- Faculty test user `78` / `test.faculty@iic-booking.test` used because student `77` was waitlisted on some gear.
- PXRD/XPS may waitlist; EPR was used for mutation E2E previously.
- Estimate ₹ may differ from domain debit for some faculty/equipment configs (report explicitly).

## Safety exclusions verified

- No Portal Migration / T0 changes for this phase
- No Legacy MySQL writes for this phase
- No Channel-I / DSA / RAA implementation changes planned unless a proven Copilot integration defect
