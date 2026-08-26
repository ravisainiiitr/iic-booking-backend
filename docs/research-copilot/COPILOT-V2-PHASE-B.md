# Copilot V2 — Phase B

## Scope
Controlled **booking** mutations only:
- CREATE booking (prepare → confirm → execute)
- CANCEL booking
- RESCHEDULE booking

**Wallet recharge/credit = Phase C (OFF).**

## Architecture
Copilot orchestrates; domain engines stay authoritative:
- Create → `_book_equipment_impl` via synthetic authenticated request
- Cancel → `user_cancel_booking` / `perform_booking_cancellation` path
- Reschedule → `user_reschedule_booking`

## Feature flags (default False)
- `COPILOT_BOOKING_CREATE`
- `COPILOT_BOOKING_CANCEL`
- `COPILOT_BOOKING_RESCHEDULE`
- `COPILOT_BOOKING_MODIFY` (unused)
- Wallet flags remain False

Prepare/proposal UX works with flags OFF. **Execute is blocked** until flags are enabled after controlled E2E.

## Confirmation
Proposals stored in cache (`proposal_id`, `confirmation_token`, `expires_at`, user binding, payload fingerprint).
Confirm via chat (“Confirm”) or `POST /api/v1/research-copilot/mutations/confirm/`.

## Idempotency
`idempotency_key` (default `copilot:{user}:{action}:{proposal_id}`) caches successful execute results for 24h.

## Deploy posture
Ship code with **all mutation flags OFF**. Enable only after Main Administrator approval following controlled E2E on a test account.
