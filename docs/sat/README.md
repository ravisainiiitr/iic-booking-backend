# Remote Analysis — System Acceptance Testing (SAT)

**Status:** Framework ready · Execution pending  
**Scope:** Portal (`iic_booking.remote_analysis`) + Agent (`RemoteAnalysisAgent`)  
**Rule:** Do **not** add product features during SAT. Fix defects only.

## Deliverables

| # | Document | Path |
|---|----------|------|
| 1 | System Acceptance Test Plan | [00-System-Acceptance-Test-Plan.md](00-System-Acceptance-Test-Plan.md) |
| 2 | Detailed checklist | [01-Detailed-Checklist.md](01-Detailed-Checklist.md) |
| 3 | Pass / Fail criteria | [02-Pass-Fail-Criteria.md](02-Pass-Fail-Criteria.md) |
| 4 | Expected API sequence | [03-Expected-API-Sequence.md](03-Expected-API-Sequence.md) |
| 5 | Expected database changes | [04-Expected-Database-Changes.md](04-Expected-Database-Changes.md) |
| 6 | Expected Windows filesystem | [05-Expected-Windows-Filesystem.md](05-Expected-Windows-Filesystem.md) |
| 7 | Expected logs | [06-Expected-Logs.md](06-Expected-Logs.md) |
| 8 | Recovery procedures | [07-Recovery-Procedures.md](07-Recovery-Procedures.md) |
| 9 | Performance baseline | [08-Performance-Baseline.md](08-Performance-Baseline.md) |
| 10 | Known limitations | [09-Known-Limitations.md](09-Known-Limitations.md) |
| — | Production Readiness Report (after all SAT pass) | [10-Production-Readiness-Report.md](10-Production-Readiness-Report.md) |
| — | Live first-workstation commissioning (Phase 2) | [../RemoteAnalysisLiveCommissioning.md](../RemoteAnalysisLiveCommissioning.md) · [12-Live-Commissioning-Report.md](12-Live-Commissioning-Report.md) |
| — | Guacamole Session SAT (Phase 3) | [13-Guacamole-Session-SAT.md](13-Guacamole-Session-SAT.md) |

Related operator guide: [../RemoteAnalysisCommissioning.md](../RemoteAnalysisCommissioning.md)  
Guacamole architecture: [../RemoteAnalysisGuacamoleArchitecture.md](../RemoteAnalysisGuacamoleArchitecture.md)

## Suites

| ID | Suite | Auto | Lab |
|----|-------|------|-----|
| SAT-01 | Agent Registration | ✓ | ✓ |
| SAT-02 | Heartbeat | ✓ | ✓ |
| SAT-03 | Workspace Lifecycle | ✓ | ✓ |
| SAT-04 | File Synchronization | ✓ | ✓ (large / interrupt) |
| SAT-05 | Remote Analysis Workflow (E2E) | ✓ (portal) | ✓ (live agent) |
| SAT-06 | Failure Recovery | partial | ✓ |
| SAT-07 | Security | ✓ | ✓ (hijack / CSRF browser) |
| SAT-08 | Performance | — | ✓ (`SAT_PERF=1`) |
| SAT-09 | Database Integrity | ✓ | ✓ |
| SAT-10 | Audit | ✓ | ✓ |
| SAT-11 | Guacamole Session | ✓ (mock) | ✓ (`SAT_GUAC=1`) |

## How to run

### Automated SAT (CI / local, no agent)

```bash
cd iic-booking-backend
venv\Scripts\python.exe -m pytest iic_booking/remote_analysis/tests/sat -m sat -q
```

### Lab SAT (live Analysis PC + portal)

```bash
set SAT_LAB=1
venv\Scripts\python.exe -m pytest iic_booking/remote_analysis/tests/sat -m "sat or sat_lab" -q
```

### Performance SAT

```bash
set SAT_PERF=1
venv\Scripts\python.exe -m pytest iic_booking/remote_analysis/tests/sat -m sat_perf -q
```

### Manual checklist

Print or edit [01-Detailed-Checklist.md](01-Detailed-Checklist.md). Mark each row **PASS / FAIL / N/A** with evidence (ticket, screenshot, log excerpt, commit SHA).

## Exit criteria

SAT is **complete** only when:

1. Every checklist row is PASS or documented N/A with owner approval.
2. Automated `sat` suite is green on the release candidate commit.
3. Lab suites SAT-05 (live) and SAT-06 critical paths are signed.
4. No open **Sev-1 / Sev-2** defects (see Pass/Fail criteria).
5. [10-Production-Readiness-Report.md](10-Production-Readiness-Report.md) sign-off section is completed.
