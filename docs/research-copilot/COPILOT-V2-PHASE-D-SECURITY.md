# Copilot V2 Phase D — Security

## Authoritative identity

Authenticated portal user is the only identity for bookings, wallet, results, RAA, tickets. LLM cannot select another user.

## Controls preserved from A/B/C

- Proposal + confirmation token for mutations
- Idempotency keys on execute
- Mutation feature flags default OFF
- Test-account E2E gate (`COPILOT_BOOKING_E2E_TEST_MODE`) fail-closed
- Wallet recharge/credit never auto-approve credit

## Phase D additions

| Risk | Mitigation |
|------|------------|
| Ticket spam | `COPILOT_TICKET_CREATE=false`; assist deep-links only |
| Analysis mutations | `COPILOT_ANALYSIS_ACTIONS=false` |
| Cross-user equipment lists | Catalog is shared public/auth data; personal tools still user-scoped |
| Prompt injection → fake slots | Slots/prices only from domain tools; unanswered path on gap |
| Ordinal context confusion | Choices stored server-side per conversation cache; not client-trusted IDs alone for mutations (mutations re-resolve via proposals) |

## Required before enabling new mutation flags

- IDOR checks on any new mutating endpoints
- Explicit confirmation UX
- Controlled test-account E2E

## Verdict note

Security posture for **reads** is consistent with Phase A. New mutation surfaces remain disabled.
