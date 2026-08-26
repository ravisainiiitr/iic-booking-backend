# Copilot V2 — Booking E2E (Phase B)

## Controlled test account only
Do not use arbitrary production user data.

## Preferred enablement during qualification (NOT global)

Keep global flags OFF:

```
COPILOT_BOOKING_CREATE=false
COPILOT_BOOKING_CANCEL=false
COPILOT_BOOKING_RESCHEDULE=false
COPILOT_WALLET_RECHARGE=false
COPILOT_WALLET_CREDIT=false
```

Enable execute **only** for an allowlisted `is_test_account` user:

```
COPILOT_BOOKING_E2E_TEST_MODE=true
COPILOT_BOOKING_TEST_USER_IDS=<test-user-pk>
```

Rules:
- User **must** have `is_test_account=True`
- User **must** be in `COPILOT_BOOKING_TEST_USER_IDS`
- Empty allowlist = nobody (fail closed)
- Real users never admitted via E2E mode
- Wallet mutation flags are never enabled via E2E mode

After qualification, set `COPILOT_BOOKING_E2E_TEST_MODE=false` and clear the allowlist.
Global booking mutation flags stay OFF until Main Administrator approval.

## Flow
1. Login (dedicated test account)
2. Ask about FESEM / XRD equipment
3. Find available slots
4. Estimate price
5. “Book it” → proposal card
6. Soft phrases (“okay”) must NOT execute
7. Explicit Confirm → create via existing `_book_equipment_impl`
8. Verify booking in My Bookings + Copilot
9. Verify wallet impact via portal (existing debit path)
10. Reschedule → confirm → verify
11. Cancel → confirm → verify
12. Replay confirm with same idempotency key → one mutation
13. Verify audit (`TOOL_EXECUTED` details without secrets)
14. Disable E2E mode / restore flags

## Enablement gate (production-wide)
Only after controlled E2E passes may Main Administrator enable:
- `COPILOT_BOOKING_CREATE`
- `COPILOT_BOOKING_CANCEL`
- `COPILOT_BOOKING_RESCHEDULE`

Wallet mutation flags remain OFF (Phase C).
