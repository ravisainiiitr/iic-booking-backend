# Copilot V2 Tool Registry (Phase A)

## Read tools (enabled)
| Tool | Auth | Source |
|------|------|--------|
| search_equipment / resolve_equipment | * | Equipment catalog |
| search_available_slots | * | DailySlot / portal slot semantics |
| estimate_booking_cost | * | ChargeCalculationEngine |
| search_bookings / get_next_booking | auth | Caller bookings |
| get_wallet / wallet_transactions | auth | Accessible wallet |
| get_sample_status / get_booking_results | auth | Own booking |
| get_remote_analysis_status | auth | BookingRemoteAnalysisService |
| get_affiliations | auth | User affiliations |
| get_pending_actions | auth | Aggregated pending |
| search_documentation | * | RAG (published only) |

## Mutations (Phase B/C — FLAGS OFF)
create/cancel/reschedule booking, wallet recharge/credit — scaffolds only; confirmation + idempotency required when enabled.

### Enablement gates (do not flip in Phase A)
1. Phase A acceptance green on staging/prod-like flags
2. Explicit env: `COPILOT_BOOKING_CREATE|CANCEL|RESCHEDULE|MODIFY`, `COPILOT_WALLET_RECHARGE|CREDIT`
3. Confirmation token + idempotency key on every execute
4. `research_copilot_mutation` throttle (strict)
5. Code paths: `services/v2/mutations/booking.py`, `services/v2/mutations/wallet.py`
