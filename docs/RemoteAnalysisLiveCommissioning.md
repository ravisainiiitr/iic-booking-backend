# Phase 2 — Live First Workstation Commissioning

**Status:** In progress  
**Rule:** No speculative features. Fix only defects found during this live run.  
**Goal:** One complete Portal → Agent → Portal cycle on a real Analysis PC.

For **Phase 4 production commissioning** (Live dashboard, fault injection, readiness checklists), see [RemoteAnalysisPhase4LiveCommissioning.md](RemoteAnalysisPhase4LiveCommissioning.md).

Portal commit baseline: Phase 1 (`ba9a053` or later on `main`).

---

## Entry criteria

- [ ] Portal deploy includes Phase 1 (toolkit + commissioning console + migrations through `0011+`)
- [ ] Agent service installed on Analysis PC; enrollment key configured if required
- [ ] Admin account with Remote Analysis manage permission
- [ ] Equipment with `enable_remote_analysis=True`
- [ ] Sample input + sample/dummy output files prepared

## URLs

| Tool | Path |
|------|------|
| Toolkit | `/api/v1/analysis/operations/toolkit/?view=html` |
| Sync Commissioning Console | `/api/v1/analysis/operations/commissioning/?view=html` |
| Run evidence ZIP | `/api/v1/analysis/operations/toolkit/runs/<run_id>/evidence/` |
| Legacy diagnostics | `/api/v1/analysis/operations/diagnostics/?view=html` |

Observability (timeline + evidence): [RemoteAnalysisCommissioningObservability.md](RemoteAnalysisCommissioningObservability.md). Prefer starting a run via `POST …/toolkit/runs/` before the live cycle so all audits share one `CommissioningRunId`.

Portal migrations for this feature: through `0011_commissioning_run_observability`.

Agent paths (Analysis PC):

- State: `C:\ProgramData\RemoteAnalysisAgent\State\agent-state.json`
- Logs: `C:\ProgramData\RemoteAnalysisAgent\Logs\raa-*.log`
- Session: `C:\ProgramData\RemoteAnalysisAgent\Sessions\<reservation_id>\`

---

## Live checklist

Mark **PASS / FAIL / N/A** with UTC timestamps. On FAIL → stop → capture defect (below).

### 1. Workstation status

| Check | Result | Time (UTC) | Evidence |
|-------|--------|------------|----------|
| Online | | | Toolkit Overview / Agent tab |
| Heartbeat current (age ≤ 90s) | | | |
| Health GREEN (or acceptable score) | | | |

### 2. Toolkit

| Check | Result | Time | Evidence |
|-------|--------|------|----------|
| Connectivity Suite overall PASS | | | JSON / screenshot |
| Full Self-Test overall PASS | | | |
| Commissioning PDF exported | | | Filename |

### 3–5. Booking → workspace → input

| Check | Result | Time | IDs / notes |
|-------|--------|------|-------------|
| Real COMPLETED booking created | | | booking_id= |
| Workspace generated | | | workspace_id= reservation_id= |
| Real input uploaded (portal) | | | relative_path= sha256= |
| Workspace created (DB) | | | |
| Input downloaded to agent `Input\` | | | path on PC |
| Checksums match portal ↔ agent | | | |

### 6–7. Analysis pause

| Check | Result | Time | Notes |
|-------|--------|------|-------|
| Operator confirmed analysis software / manual Output ready | | | Operator: |
| Output file present under agent `Output\` | | | filename= |

### 8–9. Collect + results

| Check | Result | Time | Notes |
|-------|--------|------|-------|
| Collect issued / command COMPLETED | | | command_id= |
| Output uploaded to portal Processed/ | | | sha256= |
| Checksums verified | | | |
| Results visible on booking | | | |
| Files downloadable from portal | | | |

### 10–11. Cleanup

| Check | Result | Time | Notes |
|-------|--------|------|-------|
| Cleanup issued / COMPLETED | | | command_id= |
| Session folder removed (or policy-compliant) | | | |
| No orphan workspace / PENDING cmds | | | |
| Workstation **AVAILABLE** | | | |
| Audit trail complete | | | |

### SAT-05

| Check | Result |
|-------|--------|
| SAT-05 marked PASS in `docs/sat/01-Detailed-Checklist.md` | |

---

## Defect capture (copy per failure)

```
ID: LIVE-00N
Step: (checklist #)
Severity: Critical | High | Medium | Low
Error message:
API request/response:
Portal logs:
Agent logs (raa-*.log excerpt):
DB state (workspace sync_phase, commands, files):
Agent state (status, session folder listing):
Root cause:
Smallest fix proposed:
Regression test:
```

---

## Success criteria (all required)

- [ ] Complete Input → Output → Cleanup cycle
- [ ] No manual DB intervention
- [ ] No service restart
- [ ] No orphan workspaces
- [ ] No orphan files
- [ ] Workstation automatically Available
- [ ] Result downloadable from portal
- [ ] Commissioning PDF signed
- [ ] SAT-05 PASS

When all boxes are checked, fill [12-Live-Commissioning-Report.md](sat/12-Live-Commissioning-Report.md).
