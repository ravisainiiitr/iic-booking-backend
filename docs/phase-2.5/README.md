# Phase 2.5 — Stabilization & System Acceptance

**Status:** Laboratory SAT Execution Mode (no new business features)  
**Scope:** Phase 1 Plug-and-Play + Phase 2 Enterprise Lifecycle  
**Rule:** Code changes only for verified SAT defects. No automatic commits.

## Deliverables index

| Document | Path |
|----------|------|
| SAT Master Test Plan | [SAT-Master-Test-Plan.md](./SAT-Master-Test-Plan.md) |
| UAT Test Plan | [UAT-Test-Plan.md](./UAT-Test-Plan.md) |
| Integration Test Plan | [Integration-Test-Plan.md](./Integration-Test-Plan.md) |
| Performance Test Plan | [Performance-Test-Plan.md](./Performance-Test-Plan.md) |
| Security Test Plan | [Security-Test-Plan.md](./Security-Test-Plan.md) |
| Final Acceptance Checklist | [Final-Acceptance-Checklist.md](./Final-Acceptance-Checklist.md) |
| Production Readiness Report | [Production-Readiness-Report.md](./Production-Readiness-Report.md) |
| Code Review Summary | [Code-Review-Summary.md](./Code-Review-Summary.md) |
| Lab SAT Execution Mode | [Lab-SAT-Execution-Mode.md](./Lab-SAT-Execution-Mode.md) |
| Deployment Audit (2026-08-04) | [Deployment-Audit-Report-2026-08-04.md](./Deployment-Audit-Report-2026-08-04.md) |
| Release Preparation / commit plan | [Release-Preparation-Report-2026-08-04.md](./Release-Preparation-Report-2026-08-04.md) |
| **Phase 2.5 RC1 release engineering** | [`docs/release/phase-2.5-rc1/`](../release/phase-2.5-rc1/README.md) |
| **Phase 2.6 Repository Recovery** | [`docs/phase-2.6/`](../phase-2.6/README.md) |

## Control planes

```
Portal → DSA → Equipment PC
Portal → RAA → Analysis PC
```

## GO status

| Gate | Status |
|------|--------|
| Critical defects | **0 open** (verify in lab SAT) |
| High pending (H-06, H-10, H-11) | Optimization / completeness — track as defects if SAT fails |
| Overall | **Pending** until Lab SAT evidence → then GO / Conditional GO / NO GO |

See [Production-Readiness-Report.md](./Production-Readiness-Report.md).

## Lab SAT Execution

| Resource | Path |
|----------|------|
| Main Admin UI | `/test-dashboard` |
| API | `/api/v1/lab/testing/` |
| Operator guide | [Lab-SAT-Execution-Mode.md](./Lab-SAT-Execution-Mode.md) |
| Catalog | `SAT_CATALOG` + `EXECUTION_PLAN` (51 cases, Stages 1–5) |

Wizard: current test → preconditions → steps → expected → Pass/Fail/Blocked/Skip → evidence → defects → next.  
Reports: CSV / Excel / PDF. Live health panel + readiness score + final checklist.

## Related existing SAT (Remote Analysis)

[`docs/sat/`](../sat/README.md) remains valid for detailed RA sync SAT.

## Related documentation

| Phase | Index |
|-------|-------|
| Phase 1 Plug-and-Play | [`docs/plug-and-play/`](../plug-and-play/README.md) |
| Phase 2 Enterprise | [`docs/enterprise/`](../enterprise/README.md) |

## Recommended lab execution order

1. Stage 1 Deployment + Commissioning (SAT-COM-001…003)
2. Stage 2 DSA / Equipment PC / Synchronization
3. Stage 3 Booking → Remote Analysis → Tunnel → Guacamole → Results
4. Stage 4 Fleet / Deployment Center / Diagnostics / Reporting
5. Stage 5 Performance / Security / Final Acceptance
6. Sign-off — [Final-Acceptance-Checklist.md](./Final-Acceptance-Checklist.md)
