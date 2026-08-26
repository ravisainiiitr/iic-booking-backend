# Copilot V2 — Idempotency

Every mutation execute requires an idempotency key.

Default: `copilot:{user_id}:{action}:{proposal_id}`

On success, result is cached 24h under a hash of `(user_id, key)`.
Retries (browser / network / FE) return the **same** result and must not create a second booking.

Tests cover: prepare → execute → execute again with same key → one logical outcome (`idempotent_replay=true`).
