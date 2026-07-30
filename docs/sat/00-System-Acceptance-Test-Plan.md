# 00 — System Acceptance Test Plan

## 1. Purpose

Prove that the Remote Analysis platform operates correctly from **booking creation through result download**, including agent lifecycle, heartbeats, workspace sync, security, recovery, audit, and performance under defined loads.

This plan validates the **commissioned** system. It does not introduce features.

## 2. System under test (SUT)

| Component | Location | Role |
|-----------|----------|------|
| Booking Portal | `iic-booking-backend` / `iic_booking.remote_analysis` | Source of truth: workstations, commands, workspaces, files, audit |
| Remote Analysis Agent | `RemoteAnalysisAgent` (Windows service) | Heartbeat, inventory, command execution, Input download, Output upload |
| Commissioning console | `/api/v1/analysis/operations/commissioning/` | Operator-driven Guacamole-free sync path |
| Storage | Portal media / workspace files; Agent `C:\ProgramData\RemoteAnalysisAgent\` | File bytes + local state |

**Out of scope for this SAT cycle (unless already enabled in env):** Guacamole desktop sessions as the primary commissioning path. Guacamole may be smoke-tested separately; SAT-05 primary path is **Portal ↔ Agent file sync**.

## 3. Phase mapping (requested ↔ implemented)

User-facing names map to `WorkspaceSyncPhase` / `WorkspaceStatus`:

| Requested | Implemented value | Notes |
|-----------|-------------------|-------|
| Queued | `Preparing` (legacy `QUEUED` normalized) | Reservation may be `QUEUED` separately |
| Preparing | `Preparing` | Workstation often `PREPARING` |
| DownloadingInput | `DownloadingInput` | |
| VerifyingInput | `VerifyingInput` | |
| InputReady | `InputReady` | Pause / manual analysis window |
| Running | `SessionActive` (or `SessionStarting`) | Sync-only path may stay at InputReady until Collect |
| CollectingOutput | `CollectingOutput` → `UploadingOutput` | |
| UploadVerified | `UploadVerified` | |
| Completed | `Completed` | |
| Cleaning | `Cleanup` + workstation `CLEANING` | |
| Deleted | Workspace `status=DELETED` / session folder removed | Soft-delete / archive may apply |

## 4. Environments

| Env | Portal | Agent | Data |
|-----|--------|-------|------|
| **Auto** | Django test DB | Mocked / API-only | Ephemeral |
| **Lab** | Staging portal URL | Real Analysis PC | Disposable booking + sample files |
| **Perf** | Staging dedicated | 1–20 agents or simulated | Synthetic payloads |

Record for each run: portal git SHA, agent version, migration head (`remote_analysis`), OS build.

## 5. Roles & responsibilities

| Role | Responsibility |
|------|----------------|
| SAT lead | Checklist ownership, defect triage, sign-off |
| Portal engineer | Auto suite, API/DB evidence |
| Agent engineer | Lab filesystem, agent logs, restart scenarios |
| Security reviewer | SAT-07 evidence |
| Ops | SAT-06 infrastructure restarts, monitoring |

## 6. Execution order

1. **SAT-01** Registration (blocks all agent work)
2. **SAT-02** Heartbeat / health
3. **SAT-07** Security (early; blocks production)
4. **SAT-03** Lifecycle phases (API + DB)
5. **SAT-04** File sync matrix
6. **SAT-05** Full workflow (commissioning)
7. **SAT-09** DB integrity on residual state
8. **SAT-10** Audit completeness
9. **SAT-06** Failure recovery
10. **SAT-08** Performance baselines
11. Fill **Production Readiness Report**

## 7. Evidence requirements

For each FAIL: defect ID, severity, repro steps, logs, expected vs actual.  
For each PASS (lab): timestamp, actor, workstation `agent_id`, workspace UUID, command IDs, SHA-256 of sample files.

## 8. Automation map

| Suite | Pytest module |
|-------|---------------|
| SAT-01 | `tests/sat/test_sat_01_registration.py` |
| SAT-02 | `tests/sat/test_sat_02_heartbeat.py` |
| SAT-03 | `tests/sat/test_sat_03_lifecycle.py` |
| SAT-04 | `tests/sat/test_sat_04_filesync.py` |
| SAT-05 | `tests/sat/test_sat_05_workflow.py` |
| SAT-06 | `tests/sat/test_sat_06_recovery.py` |
| SAT-07 | `tests/sat/test_sat_07_security.py` |
| SAT-08 | `tests/sat/test_sat_08_performance.py` |
| SAT-09 | `tests/sat/test_sat_09_database.py` |
| SAT-10 | `tests/sat/test_sat_10_audit.py` |

## 9. Entry criteria

- [ ] Migrations applied through `0010_workspace_lifecycle_phases` (or later)
- [ ] Commissioning console auth UX deployed (session + HTML login redirect)
- [ ] Agent commissioned on lab PC (enrollment key configured when `DEBUG=False`)
- [ ] At least one COMPLETED booking with `enable_remote_analysis=True` (lab)
- [ ] Unit/integration remote_analysis tests green on RC commit

## 10. Exit criteria

See [02-Pass-Fail-Criteria.md](02-Pass-Fail-Criteria.md) and [README.md](README.md).
