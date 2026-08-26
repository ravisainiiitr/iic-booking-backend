# Copilot V2 Phase D.1 — GO / NO-GO

## Decision

# READY FOR CONTROLLED PRODUCTION PILOT

## What “GO” means

- Dedicated / pilot users may exercise **read + booking prepare/confirm** paths under existing Phase B enablement rules (Main Admin turns global booking flags ON only when ready).
- Copilot remains an **orchestrator** over portal domain services.
- Financial mutation flags stay **OFF**.
- Analysis/ticket mutation flags stay **OFF**.

## What “GO” does **not** mean

- Not global enablement by default.
- Not Phase C wallet recharge/credit enablement.
- Not a guarantee that every XRD instrument has bookable far slots.
- Not a substitute for a clean tagged production deploy of Phase D/D.1 sources.

## Evidence summary

| Gate | Status |
|------|--------|
| Phase A regression | PASS (66 combined A–D OK) |
| Phase B regression | PASS |
| Phase C read regression | PASS |
| Phase D regression | PASS |
| Corpus smoke ≥55% deterministic | PASS (~75.7%) |
| Equipment discovery (XRD) | PASS |
| Ordinal memory | PASS |
| Slot search deterministic | PASS (0 tomorrow slots reported honestly) |
| Cost estimate | PASS |
| Wallet read | PASS |
| Booking E2E | PASS (booking 458) |
| Reschedule E2E | PASS |
| Cancel E2E | PASS |
| Idempotency | PASS |
| Security (foreign/token) | PASS |
| Audit | PASS |
| RAA | PASS (authoritative ineligible; no fabrication) |
| Frontend browser E2E | NOT RUN (API confirm path exercised) |
| Unintended production flag changes | NONE |
| Financial mutations | NOT ENABLED |

## Conditions before wider pilot

1. Tag/deploy Phase D+D.1 backend (and FE comparison cards) cleanly — not only docker cp.
2. Keep wallet mutation flags OFF.
3. Monitor KnowledgeGap / unanswered for XRD slot emptiness vs missing catalog content.
4. Optional: Main Admin enables global booking flags only after reviewing Phase B + D.1 reports.

## Rollback

Restore previous container image/tag; leave all mutation flags false.
