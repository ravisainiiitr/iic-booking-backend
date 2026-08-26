# Copilot V2 — Confirmation Model

1. User requests book / cancel / reschedule (deterministic intent).
2. Backend validates and creates a **proposal** (cache, 15 min TTL).
3. UI shows proposal card + Confirm / Change.
4. Explicit confirm only:
   - Button → `POST /mutations/confirm/` with `proposal_id` + `confirmation_token`
   - Or chat phrase: Confirm / Yes, book it / Proceed / Book this
5. Soft phrases (“okay”, “looks good”, “maybe”) do **not** confirm.
6. Before execute: re-check ownership, proposal token, expiry, and (for create) slot still AVAILABLE.
7. Stale / changed proposals cannot be confirmed; user must prepare again.
