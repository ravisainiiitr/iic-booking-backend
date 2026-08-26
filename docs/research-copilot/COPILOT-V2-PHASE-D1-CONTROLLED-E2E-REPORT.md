# Copilot V2 Phase D.1 — Controlled E2E Report

**Date:** 2026-08-26 (UTC)  
**Method:** Dedicated test-account gate only (`COPILOT_BOOKING_E2E_TEST_MODE` process env for the runner; **not** persisted)  
**Global mutation flags before/after:** unchanged OFF  

## Verdict

**READY FOR CONTROLLED PRODUCTION PILOT**

This is **not** global enablement. Financial mutations remain OFF. Global `COPILOT_BOOKING_*` remain OFF until Main Admin explicitly enables them.

## Exact SHAs / tags

| Component | Value |
|-----------|--------|
| Production container baseline tag | `v2.5.44-copilot-v2-phase-c` (pre-sync) |
| Phase D/D.1 code on container | Synced via docker cp for qualification (capability/multi-intent/unanswered/orchestrator/intent/read_tools/D1 command) |
| Local backend working tree | `498f87ea` + uncommitted Phase D/D.1 changes at run time |
| Frontend SHA (repo) | `99b378a6cdd1c455330035d18dea68db9b541f9b` (Phase C cards; D.1 FE comparison cards present locally, browser click-through **not** automated) |

Evidence file: `COPILOT-V2-PHASE-D1-E2E-EVIDENCE.json`

## Flags (persistent container env — verified after run)

```
COPILOT_BOOKING_CREATE=false
COPILOT_BOOKING_CANCEL=false
COPILOT_BOOKING_RESCHEDULE=false
COPILOT_BOOKING_E2E_TEST_MODE=false
COPILOT_WALLET_RECHARGE=false
COPILOT_WALLET_CREDIT=false
COPILOT_WALLET_READ=true
```

Runner used **one-shot** `docker exec -e COPILOT_BOOKING_E2E_TEST_MODE=true -e COPILOT_BOOKING_TEST_USER_IDS=78` only.

## Test account (live portal — not invented)

| Field | Value |
|------|--------|
| User ID | 78 |
| Email | `test.faculty@iic-booking.test` |
| Type | faculty |
| `is_test_account` | true |
| Wallet | ₹103,616.99 (unchanged through journey) |
| Credit | Feature disabled; outstanding ₹0.00 |

## Conversational discovery (XRD)

| Step | Result |
|------|--------|
| “I need to perform XRD analysis.” | `capability_search`, 6 catalog hits, `llm_used=false` |
| “The second one.” | Selected **GI-XRD** `equipment_id=40` |
| Cross-conversation ordinal leak | None |
| “Find earliest slot tomorrow.” | Deterministic; **0** AVAILABLE tomorrow for GI-XRD (honest portal) |
| “How much will it cost?” | Estimate ₹50.00; wallet ₹103,616.99 |
| “Do I have enough?” | Live wallet read |

## Mutation path (authoritative booking services)

GI-XRD lacked ≥2 far slots outside cancel/reschedule threshold → **documented fallback** to EPR `43` (same approach as Phase B controlled E2E).

| Field | Value |
|------|--------|
| Equipment | EPR (`43`) |
| Slots | `26037` → reschedule `26038` (2026-08-31) |
| Booking | **458** / `IICEPR202600006` |
| Create | BOOKED via `POST …/mutations/confirm/` |
| Idempotent create replay | ONE booking |
| Reschedule | Verified slot `26038` |
| Cancel | Final status **REFUNDED** |
| Idempotent cancel replay | OK |
| Wallet debit | ₹0 domain charge (unchanged balance) — same class of outcome as Phase B EPR |

## Security / confirmation

| Check | Result |
|------|--------|
| Soft confirm phrases | Do not execute |
| “Book it.” | Proposal only (`ACTION_PREPARATION`) |
| Wrong token | `CONFIRMATION_INVALID` |
| Foreign user execute | `COPILOT_BOOKING_CREATE_DISABLED` |
| Prompt injection user switch | Domain uses authenticated user only |
| Cancel without confirm | Status remained BOOKED |

## Analysis / RAA

| Check | Result |
|------|--------|
| RAA status | Deterministic: **not eligible** for EPR — not fabricated |
| “Is my analysis ready?” | Initially missed intent (fixed post-run → `results`); re-classify regression added |

## Multi-intent

Compound XRD prepare query did **not** auto-execute a booking. Extra booking count = 0. Decomposition quality still partial (often collapses to a dominant read intent) — acceptable for pilot with prepare-gated mutations; tracked as improvement item.

## Performance (deterministic turns, ms)

| Step | Latency ms |
|------|------------|
| Equipment discovery | ~30 |
| Ordinal | ~2 |
| Slot search | ~33 |
| Cost estimate | ~38 |
| Wallet | ~5 |
| Prepare booking | ~50 |
| Confirm create (HTTP) | ~183 |

No LLM used on these turns (`llm_used=false`).

## Tests

- Production container: Phase A+B+C+D — **66 OK**
- Corpus smoke: 84/111 deterministic (~75.7%) on non-conversational rows
- Browser FE click-through: **not automated** (confirm path = same API as Confirm button)

## Bugs found and fixed in D.1

1. **Compare stolen by slot search** — “Compare the available XRD…” matched `available`+`xrd` → `search_slots`. Fixed: capability/compare **before** availability.
2. Journey phrase gaps — cancel it / move it / XRD analysis / current balance / analysis ready.
3. Conversation create IntegrityError (`access_mode`) — E2E uses cache-backed conversation id (no schema migration).
4. Multi-intent `book`⊂`booking` false positive — fixed earlier with word boundaries.

## Remaining caveats (do not hide)

1. Selected XRD instrument may have **no bookable far slots**; mutation path falls back to bookable equipment (EPR) — portal domain reality.
2. Frontend browser E2E not run.
3. Multi-intent planner quality still imperfect for long compound utterances.
4. Phase D/D.1 code was **hot-synced** into the running container for qualification; a proper tagged deploy should follow before wider pilot.
5. Phase C financial mutations still OFF / not qualified.

## Safety

- T0 / portal migration / legacy MySQL: **NO**
- Wallet recharge/credit mutations: **NO**
- Persistent booking flags: **unchanged OFF**
