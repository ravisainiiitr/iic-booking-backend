# Copilot V2 — Booking E2E (Phase B)

## Controlled test account only
Do not use arbitrary production user data.

## Flow
1. Login
2. Ask about FESEM / XRD equipment
3. Find available slots
4. Estimate price
5. “Book it” → proposal card
6. Confirm (only after flags ON for execute)
7. Verify booking in My Bookings
8. Verify wallet impact via portal (existing debit path)
9. Ask Copilot about booking
10. Reschedule → confirm
11. Cancel → confirm
12. Verify audit (`TOOL_EXECUTED` details without secrets)

## Enablement gate
Flags stay OFF until this E2E succeeds on a staging/prod-like environment.
Then Main Administrator may enable:
- `COPILOT_BOOKING_CREATE`
- `COPILOT_BOOKING_CANCEL`
- `COPILOT_BOOKING_RESCHEDULE`

Wallet mutation flags remain OFF (Phase C).
