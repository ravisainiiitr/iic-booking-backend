# Copilot V2 — Action Tool Registry (Phase B)

| Tool | R/W | Auth | Confirm | Idempotent | Financial risk | Flag | Domain |
|------|-----|------|---------|------------|----------------|------|--------|
| prepare_booking_create | READ+prep | yes | — | n/a | estimate only | always prepare | ChargeCalculationEngine + slots |
| execute_booking_create | WRITE | yes | yes | yes | yes (wallet debit via booking) | COPILOT_BOOKING_CREATE | `_book_equipment_impl` |
| prepare_cancellation | READ+prep | yes | — | n/a | policy note only | always prepare | Booking ownership |
| execute_booking_cancel | WRITE | yes | yes | yes | refund via portal policy | COPILOT_BOOKING_CANCEL | `user_cancel_booking` |
| prepare_reschedule | READ+prep | yes | — | n/a | no | always prepare | Booking ownership |
| execute_booking_reschedule | WRITE | yes | yes | yes | may reprice via portal | COPILOT_BOOKING_RESCHEDULE | `user_reschedule_booking` |
| wallet recharge/credit | WRITE | yes | yes | yes | yes | COPILOT_WALLET_* | Phase C only — OFF |

Authorization: authenticated Django user only. LLM-supplied `user_id` / wallet owner fields are stripped.
