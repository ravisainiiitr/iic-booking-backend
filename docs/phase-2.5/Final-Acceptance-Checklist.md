# Final Acceptance Checklist — Phase 2.5

**Date:** 2026-08-04  
**Purpose:** Gate production promotion after SAT, UAT, integration, performance, and security execution.

Check each item only when **evidence** is attached (test run ID, screenshot, log excerpt, commit SHA). Leave unchecked until verified in lab.

---

## 1. Critical defect clearance

- [ ] **C-01** — DSA `equipment_pcs` preserved in Portal heartbeat serializer — **Resolved**; SAT-COM-005 PASS
- [ ] **C-02** — Lab config push/rollback persists full profile fields — **Resolved**; SAT-DSA-002/003 PASS
- [ ] No open **Critical** defects in defect tracker

---

## 2. High-priority fixes verified

- [ ] **H-01** — Pairing fail-closed when ManagementApiKey unset — SAT-SEC-001 PASS
- [ ] **H-02** — OTP stripped from ConfigJson after validation — SAT-COM-003 PASS
- [ ] **H-04** — Loopback/status ingest requires auth — SAT-SEC-004 PASS
- [ ] **H-05** — `restart_agent` dispatches to correct agent branch — SAT-FLT-003 PASS
- [ ] **H-07/H-08** — RAA update discover/report use enrollment/agent auth — INT-07 PASS
- [ ] **H-09** — Node detail does not rebuild full fleet tree each request — SAT-FLT-002 PASS
- [ ] **H-12** — Bootstrap does not prematurely set `last_reported_*` — INT-11 PASS

---

## 3. Deployment and commissioning

- [ ] DSA clean install from Deployment Center with SHA-256 verify (SAT-DEP-001)
- [ ] Equipment PC Wizard install and DSA discovery (SAT-DEP-002, SAT-COM-002)
- [ ] RAA install, enrollment, and Online heartbeat (SAT-DEP-003)
- [ ] Full chain Portal→DSA→EqPC→RAA→Analysis PC Online (SAT-COM-001)
- [ ] Deployment Center shows checksums, compatibility matrix, repair/upgrade packages (SAT-DEP-004/005/006)

---

## 4. Configuration lifecycle

- [ ] Template apply bumps version and triggers bootstrap (SAT-DSA-002)
- [ ] DSA ack returns Applied on dashboard (SAT-DSA-002)
- [ ] Configuration rollback restores prior snapshot (SAT-DSA-003)
- [ ] Config signature validated on bootstrap document

---

## 5. Booking and synchronization

- [ ] Internal user E2E: booking → sample → raw sync → RA → results → email (SAT-BKG-001)
- [ ] Faculty E2E path (SAT-BKG-002)
- [ ] External user E2E path (SAT-BKG-003)
- [ ] DSA raw data sync after sample accept (SAT-BKG-006)
- [ ] Project and startup user paths exercised or documented N/A (SAT-BKG-004/005)

---

## 6. Remote Analysis

- [ ] Session create with reverse tunnel + Guacamole (SAT-RA-001)
- [ ] Clipboard and file transfer per policy (SAT-RA-002)
- [ ] Timeout, extension, maintenance, queue behaviors (SAT-RA-003)
- [ ] Workspace cleanup and archive (SAT-RA-004)
- [ ] Concurrent multi-PC sessions (SAT-RA-005)
- [ ] RA SAT automated suite green ([`docs/sat/`](../sat/README.md))
- [ ] Guacamole session SAT executed or N/A with approval ([`docs/sat/13-Guacamole-Session-SAT.md`](../sat/13-Guacamole-Session-SAT.md))

---

## 7. Fleet operations

- [ ] Lab Infrastructure tree statuses and auto-refresh (SAT-FLT-001)
- [ ] Node detail health fields complete (SAT-FLT-002)
- [ ] Repair / RestartAgent / RescanSoftware actions (SAT-FLT-003)
- [ ] Utilization CSV export (SAT-FLT-004)
- [ ] Critical alert email on offline detector (SAT-FLT-005)
- [ ] Health detectors job documented and scheduled (`run_lab_health_detectors`)

---

## 8. Failure recovery

- [ ] DSA stop / LAN disconnect recovery (SAT-FAIL-001)
- [ ] RAA stop / partial ProgramData loss recovery (SAT-FAIL-002)
- [ ] Reverse tunnel / Guacamole outage recovery (SAT-FAIL-003)
- [ ] Disk full / missing results folder handling (SAT-FAIL-004)
- [ ] Portal + database restart mid-operation (SAT-FAIL-005)

---

## 9. Security and RBAC

- [ ] Pairing fail-closed without ManagementApiKey (SAT-SEC-001)
- [ ] Lab Infrastructure + Test Dashboard Main Admin only (SAT-SEC-002)
- [ ] Agent auth, no plaintext secrets on disk (SAT-SEC-003)
- [ ] Status ingest not forgeable (SAT-SEC-004)
- [ ] [Security-Test-Plan.md](./Security-Test-Plan.md) SEC-01 … SEC-20 complete

---

## 10. API, database, frontend

- [ ] Lab aggregate APIs authz and validation (SAT-API-001)
- [ ] Config ack idempotent (SAT-API-002)
- [ ] Phase 1/2 migrations apply cleanly (SAT-DB-001)
- [ ] lab_infrastructure indexes and FKs verified (SAT-DB-002)
- [ ] Lab Infrastructure UX states (SAT-FE-001)
- [ ] Deployment Center + Test Dashboard responsive layout (SAT-FE-002)

---

## 11. Performance

- [ ] Lab infrastructure API p95 within target at 50 nodes (SAT-PERF-001) **or** waiver with H-10 plan
- [ ] Heartbeat burst without dropped rollups (SAT-PERF-002)
- [ ] [Performance-Test-Plan.md](./Performance-Test-Plan.md) baseline tables filled

---

## 12. UAT persona sign-off

- [ ] Main Admin scenarios (UAT-MA-*) accepted
- [ ] Dept Admin scenarios (UAT-DA-*) accepted
- [ ] Faculty scenarios (UAT-FA-*) accepted
- [ ] External scenarios (UAT-EX-*) accepted
- [ ] Operator scenarios (UAT-OP-*) accepted
- [ ] Cross-persona E2E narratives (UAT-E2E-01/02/03) accepted

---

## 13. Documentation and operations

- [ ] Phase 1 docs current ([`docs/plug-and-play/`](../plug-and-play/README.md))
- [ ] Phase 2 enterprise docs current ([`docs/enterprise/`](../enterprise/README.md))
- [ ] Reverse tunnel docs current ([`docs/ReverseTunnelArchitecture.md`](../ReverseTunnelArchitecture.md))
- [ ] Runbooks for troubleshooting linked from README
- [ ] Test dashboard catalog synced with SAT-Master-Test-Plan.md

---

## 14. Known limitations accepted

- [ ] **H-06** — DSA restart/upgrade command execution completeness — accepted with mitigation plan
- [ ] **H-10** — N+1 risks on large fleets — accepted with performance waiver or fix scheduled
- [ ] **H-11** — Diagnostics depth vs full commissioning — node-scoped scope accepted
- [ ] SMS/WhatsApp notifications deferred — accepted
- [ ] Temperature sensors deferred — accepted
- [ ] mTLS deferred — accepted
- [ ] Guacamole session recording N/A if not implemented — accepted

---

## 15. Production readiness

- [ ] [Production-Readiness-Report.md](./Production-Readiness-Report.md) completed
- [ ] [Code-Review-Summary.md](./Code-Review-Summary.md) completed
- [ ] GO/NO-GO decision recorded
- [ ] Rollback plan documented for portal migration deploy

---

## 16. Final sign-off

| Role | Name | Date | Decision (GO / Conditional GO / NO-GO) | Signature |
|------|------|------|----------------------------------------|-----------|
| SAT lead | | | | |
| Product owner | | | | |
| Security | | | | |
| Operations | | | | |
| Engineering lead | | | | |

**Conditional GO** requires: all Critical resolved, High items either PASS or explicitly accepted, and lab SAT evidence attached for SAT-COM-001, SAT-BKG-001, SAT-RA-001.
