# Copilot V2 Security (Phase A)

## Rules
- Authenticated user always from request/session — never trust LLM `user_id`
- Ownership checks on bookings, wallet, results, RA
- Public tools: docs/equipment/slots/estimate only
- Mutations: flags OFF; when enabled require confirmation + idempotency + audit

## Tests
IDOR cross-user booking/wallet; unauthorized pending actions; public cannot call wallet tools.
