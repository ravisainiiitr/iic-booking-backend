# Copilot V2 Equipment Resolution

## Confidence levels
- **EXACT** — name/code exact match (case-insensitive)
- **ALIAS** — known alias map (FESEM, SEM, XRD, PXRD, …)
- **CONTEXTUAL** — conversation metadata last equipment
- **AMBIGUOUS** — multiple candidates → clarify UI
- **NOT_FOUND** — ask user / suggest catalog search

Never silently pick an unrelated instrument.

## Aliases (seed)
Configured in `services/v2/equipment_aliases.py` (code map; no migration required for Phase A).
