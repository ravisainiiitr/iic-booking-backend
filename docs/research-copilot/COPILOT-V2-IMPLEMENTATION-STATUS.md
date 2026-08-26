# Copilot V2 Implementation Status

| Area | Status |
|------|--------|
| Phase A docs | DONE |
| Feature flags + split throttles | DONE |
| Intent / equipment / datetime resolvers | DONE |
| Deterministic read tools | DONE |
| Conversation orchestration (deterministic-first) | DONE |
| FE cards + quick-action prompt fix + rate-limit UX | DONE |
| Phase A acceptance tests | DONE (unit suite green) |
| Phase B/C mutation scaffolds | DONE — FLAGS OFF |
| Production mutation enablement | DISABLED |

## Final gate

**PHASE A READY — MUTATIONS REMAIN DISABLED**

Criteria met:
- Deterministic FESEM/slots/wallet/bookings path does not require LLM
- LLM quota is separate from chat ingress; deterministic turns skip LLM bucket
- Ambiguous equipment returns clarification (no silent wrong pick)
- Auth-scoped reads reject foreign `user_id`
- Mutation flags default False; execute returns disabled / NotImplemented

## Deploy notes

- Production tag: **`v2.5.42.2-copilot-v2-phase-a`** (hotfix over `.1` Equipment.pk + FESEM token aliases)
- No schema migration required for Phase A (conversation context uses cache)
- Backend tag deployed on EC2; FE master includes cards/quick-action fix
- Do **not** set `COPILOT_BOOKING_*` or `COPILOT_WALLET_*` in production
- GitHub Actions “Deploy Backend” token was unavailable locally; deploy performed via EC2 SSH checkout of the release tag (same compose path)
