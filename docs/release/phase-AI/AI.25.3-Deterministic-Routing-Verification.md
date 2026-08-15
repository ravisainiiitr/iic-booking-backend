# AI.25.3 — Deterministic Routing Verification

Live on permanently baked Django image (`3a72438`).

## Authoritative portal → deterministic (Ollama NOT invoked)

| Query | wall_ms | provider | tools | llm_ms |
|-------|--------:|----------|-------|-------:|
| What is my next booking? | 84 | deterministic | get_next_booking | 0 |
| What is the status of my sample? | 26 | deterministic | get_sample_status | 0 |
| How much does 5 PXRD samples cost? | 115 | deterministic | search_equipment, estimate_booking_cost | 0 |
| What is my wallet balance? | 14 | deterministic | get_wallet | 0 |
| When is PXRD available tomorrow? | 27 | deterministic | search_equipment, search_slots | 0 |
| Which software for .dm4 files? | 18 | deterministic | recommend_software | 0 |
| What should I prepare before my XRD booking? | 31 | deterministic | search_documentation | 0 |

All replies labeled **PORTAL DATA** / institute documentation — no invented prices.

## General LLM path still available

| Query | wall_ms | provider | tools | llm_ms |
|-------|--------:|----------|-------|-------:|
| Explain the difference between PXRD and GI-XRD. | 52861 | **ollama** | [] | 52830 |

Confirms AI.25.2 did **not** force every query into deterministic mode.

Note: “purpose of sample preparation for XRD” routed to documentation deterministic (policy/prep docs path) — expected Opt#2 behavior.

## Pricing authority

`How much does 5 PXRD samples cost?` → `estimate_booking_cost` via ChargeCalculationEngine path; INR amount from portal; PI not invented (`EquipmentPI=0`).

## Security

| Check | Result |
|-------|--------|
| Cross-user results | denied (`booking_not_found`) |
| Cross-user wallet | `forbidden` |
| Cancel confirmation | `requires_confirmation=true` |
| Prompt injection / secrets / Ollama URL | deterministic refuse |
| Forced PUBLIC private tools | `login_required` |

## Performance observation

Deterministic portal queries: **tens of milliseconds**.  
Genuine LLM explanation: **~53s** on frozen 1b/2CPU envelope (acceptable; under 60s timeout).
