# Copilot V2 Phase D — Query Coverage

Corpus file: `COPILOT-V2-QUERY-REGRESSION-CORPUS.json` (**118** queries).

## Families covered

| Family | Examples |
|--------|----------|
| EQUIPMENT_SEARCH | XRD/SEM/FESEM/XRF/NMR/XPS/TGA/… |
| CAPABILITY_SEARCH | crystal structure, morphology, thermal, thickness |
| EQUIPMENT_COMPARISON | Compare XRD machines |
| SLOT_SEARCH | earliest, this week, Saturday, cheapest |
| COST_ESTIMATE | samples + booking estimates |
| BOOKING | prepare / cancel / reschedule / confirm |
| BOOKING_VIEW | my bookings / next booking |
| WALLET | balance, tx, spend, recharge prep, credit |
| ANALYSIS | results, sample, RAA status |
| USER_OPS | profile, affiliation, dashboard, tickets |
| INFORMATION | SOP / HOLD / sample prep (RAG) |
| CONVERSATIONAL | ordinals, multi-intent, unanswered |

## Success measurement

- Unit smoke: deterministic hit-rate threshold on non-conversational corpus rows (`test_copilot_v2_phase_d.py`).
- Production: Main Admin unanswered queue + helpful/not-helpful feedback.

## Known gaps (honest)

- Affiliation *mutations* (send/cancel joining) still portal UI-driven.
- File download / email-result / software availability are status + deep-link oriented, not full RAA mutation orchestration.
- “Two consecutive slots” / “cheapest slot” heuristics may need richer slot ranking.
- Full live multi-step pilot journey (equipment→slot→cost→wallet→book→reschedule→cancel→finance→RAA) not re-run as a single Phase D E2E on production in this deliverable.
