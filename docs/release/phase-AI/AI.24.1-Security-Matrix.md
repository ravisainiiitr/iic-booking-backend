# AI.24.1 — Security Matrix

**Principle:** Backend authorization is authoritative. The LLM only uses tools the backend already allowed.

| # | Scenario | Expected | Enforcement |
|---|----------|----------|-------------|
| 1 | Anonymous opens Copilot | Allowed when PUBLIC_ENABLED | API `AllowAny` + feature gate |
| 2 | Anonymous: “What is XRD?” | Answer (public knowledge/tools) | PUBLIC tools + RAG PUBLIC docs |
| 3 | Anonymous: public equipment | Catalogue fields only | `search_equipment` PUBLIC |
| 4 | Anonymous: public pricing | EXTERNAL/STANDARD catalogue or sign-in hint | `estimate_booking_cost` + `public_catalogue` |
| 5 | Anonymous: personalized pricing / “my rate” | Login required | intent gate + no PI path |
| 6 | Anonymous: my booking / sample / result / wallet | Login required message | `private_intent_requires_login` |
| 7 | Anonymous: invoke `get_wallet` / bookings / cancel / RA launch | Rejected **before** private DB | `execute_tool` ACL |
| 8 | Anonymous: slots / availability | Login / not public | `search_slots` AUTHENTICATED |
| 9 | Anonymous: RAA hostname / IP / Guacamole / tunnel | Never exposed | knowledge PUBLIC only + `strip_internal_infra` + security_refusal |
| 10 | Anonymous: Ollama URL / API keys / secrets | Refusal | `security_refusal` |
| 11 | Anonymous: prompt injection (“ignore instructions…”) | Safe refusal / no private data | ACL + refusal |
| 12 | Authenticated pilot: my booking / pricing / RA | AI.23 behavior | AUTHENTICATED mode |
| 13 | User A → User B booking/result/wallet | Denied | ownership in tools |
| 14 | Non-pilot authenticated | Public tools only | `effective_access_mode=public` |
| 15 | Auth transition anon → user | New conversation context | frontend clears + separate FKs |
| 16 | Cache / conversation leakage | Anon key scoped; user scoped | model filters |
| 17 | Anonymous rate flood | 429 / throttle | `research_copilot_anon` |
| 18 | Ollama busy | Controlled busy message | existing concurrency gate MAX_CONCURRENT |
| 19 | Mutations from Copilot | Confirmation cards only | MUTATION tools |
| 20 | Feedback API | Authenticated only | `IsAuthenticated` |

## Tool access levels

| Tool | Level |
|------|-------|
| search_equipment | PUBLIC |
| search_documentation | PUBLIC |
| recommend_software | PUBLIC |
| estimate_booking_cost | PUBLIC (catalogue when public mode) |
| search_slots | AUTHENTICATED |
| search_bookings | AUTHORIZED_RESOURCE |
| get_next_booking | AUTHORIZED_RESOURCE |
| get_wallet | AUTHORIZED_RESOURCE |
| get_sample_status | AUTHORIZED_RESOURCE |
| get_booking_results | AUTHORIZED_RESOURCE |
| get_sample_deadline | AUTHORIZED_RESOURCE |
| create_booking | MUTATION |
| cancel_booking | MUTATION |
| create_support_ticket | MUTATION |
| launch_remote_analysis | MUTATION |

## Acceptance (security)

- [x] Tool ACL declared and enforced in registry
- [x] Anonymous private tool path rejected before handlers
- [x] Login-required CTA for private intents
- [x] Infra/secret strip defense-in-depth
- [ ] Full automated matrix green on Postgres CI (pending env)
- [ ] Live EC2 anonymous probe (pending deploy approval)
