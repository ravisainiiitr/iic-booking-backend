# Lab SAT Execution Mode

**Status:** Active  
**Rule:** No new business features. Code changes only for verified SAT defects.  
**Commits:** Do not create until Critical=0, High=0, SAT complete, and Production Readiness = **GO** — then wait for explicit approval.

## Purpose

Execute System Acceptance Testing in a real laboratory using the Main Admin **Lab SAT Execution** UI (`/test-dashboard`).

## Operator flow

1. Apply migrations: `lab_infrastructure.0003_sat_execution_mode`
2. Open `/test-dashboard` as Main Admin
3. Optionally set Building / Floor / Lab context
4. **Start Lab SAT Run**
5. Follow the wizard (Stage 1 → 5). For each case: Preconditions → Steps → Expected → Pass/Fail/Blocked/Skip
6. Attach screenshots, logs, configs, network captures
7. On Fail: optionally create a defect (Bug / Config / Hardware / Network / User Error)
8. Watch live health + fleet highlights while executing
9. Download SAT Report (CSV / Excel / PDF)
10. Review Production Readiness score + GO / Conditional GO / NO GO checklist

## Stage order

| Stage | Focus |
|-------|--------|
| 1 | Deployment + SAT-COM-001…003 |
| 2 | DSA / Equipment PC / Sync / recovery |
| 3 | Booking + Remote Analysis + tunnel/Guac |
| 4 | Fleet / Deployment Center / Diagnostics / Reporting |
| 5 | Performance / Security / Final platform checks |

## APIs

| Method | Path |
|--------|------|
| GET | `/api/v1/lab/testing/` |
| GET | `/api/v1/lab/testing/wizard/` |
| POST | `/api/v1/lab/testing/evidence/` |
| GET/POST | `/api/v1/lab/testing/defects/` |
| GET | `/api/v1/lab/testing/health/` |
| GET | `/api/v1/lab/testing/readiness/` |
| GET | `/api/v1/lab/testing/runs/{id}/report/?format=json\|csv\|xlsx\|pdf` |

## Related plans

Full case sheets remain in [SAT-Master-Test-Plan.md](./SAT-Master-Test-Plan.md) and prior RA SAT under [`docs/sat/`](../sat/README.md).
