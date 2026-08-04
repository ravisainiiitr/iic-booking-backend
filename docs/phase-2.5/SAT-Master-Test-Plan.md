# SAT Master Test Plan — Phase 2.5

**Date:** 2026-08-04  
**Scope:** Phase 1 Plug-and-Play + Phase 2 Enterprise Lifecycle (stabilization only; no new business features)  
**Control planes:** Portal→DSA→Equipment PC · Portal→RAA→Analysis PC  
**Related:** RA-focused SAT under [`docs/sat/`](../sat/README.md) — remain valid; this plan extends coverage to DSA, Deployment Center, Lab Infrastructure, and full booking E2E.

---

## 1. Purpose

Execute system acceptance testing (SAT) across the full laboratory platform before production promotion. Phase 2.5 validates defect fixes, cross-component integration, security hardening, and operational readiness without introducing product features.

## 2. System under test

| Layer | Components |
|-------|------------|
| Portal | Booking, sync, remote analysis, deployment, lab infrastructure |
| DSA | Department Sync Agent — discovery, pairing, config push, equipment rollup |
| Equipment PC | Configuration Wizard, folder/share layout, status posts to DSA |
| RAA | Remote Analysis Agent — enrollment, heartbeat, reverse tunnel, workspace sync |
| Infrastructure | Guacamole gateway, reverse tunnel, PostgreSQL, Celery, email |

## 3. Roles and responsibilities

| Role | Responsibility |
|------|----------------|
| SAT lead | Owns checklist, defect triage, sign-off |
| Portal engineer | API/DB evidence, automated suites |
| DSA / Wizard engineer | LAN discovery, config pack, EqPC lifecycle |
| RAA engineer | Agent logs, tunnel, Guacamole, workspace sync |
| Security reviewer | SAT-SEC-* evidence |
| Ops | Failure recovery drills, portal/DB restarts |

## 4. Environments

| Environment | Portal | Agents | Data |
|-------------|--------|--------|------|
| **Auto** | Django test DB | Mocked / API-only | Ephemeral |
| **Lab** | Staging portal | Real DSA, EqPC, Analysis PC | Disposable bookings |
| **Perf** | Staging dedicated | Simulated or ≥50 nodes | Synthetic load |

Record for each run: portal git SHA, agent versions, migration heads, OS builds.

## 5. Execution order (recommended)

1. **Deployment** (SAT-DEP-*) — installers and Deployment Center
2. **Security early gate** (SAT-SEC-001 … SAT-SEC-004)
3. **Commissioning chain** (SAT-COM-*)
4. **Configuration push** (SAT-DSA-002, SAT-DSA-003)
5. **Booking E2E** (SAT-BKG-*)
6. **Remote Analysis** (SAT-RA-*; detailed steps in [`docs/sat/`](../sat/01-Detailed-Checklist.md))
7. **Fleet / Lab UI** (SAT-FLT-*)
8. **Failure recovery** (SAT-FAIL-*)
9. **API / DB / Frontend** (SAT-API-*, SAT-DB-*, SAT-FE-*)
10. **Performance** (SAT-PERF-*; see [Performance-Test-Plan.md](./Performance-Test-Plan.md))

## 6. Evidence requirements

For each **FAIL**: defect ID, severity, repro steps, logs, expected vs actual.  
For each **PASS** (lab): timestamp, actor, node IDs, command IDs, SHA-256 of installers, screenshot or log excerpt.

## 7. Test dashboard

| Resource | Path |
|----------|------|
| UI | `/test-dashboard` (Main Admin) |
| API | `/api/v1/lab/testing/` |
| Catalog seed | `iic_booking.lab_infrastructure.services.testing.SAT_CATALOG` |

Keep this document in sync with the catalog when adding or retiring test IDs.

---

## 8. Master test case catalog

**Legend:** Actual Result, Status, and Remarks are left blank for lab execution (TBD).

### 8.1 Deployment

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-DEP-001 | Deployment | Clean Windows DSA install from Deployment Center | Clean Win10/11 VM; Main Admin; published DSA release with SHA-256 | 1. Download DSA via ticket<br>2. Verify SHA-256<br>3. Install silently/wizard<br>4. Confirm service starts | DSA service running; LocalApi listens; no manual config beyond enrollment secret | | | critical | |
| SAT-DEP-002 | Deployment | Equipment PC Wizard install + integrity | Published Wizard release in Deployment Center | 1. Download Wizard via ticket<br>2. Verify SHA-256<br>3. Install and launch | Wizard launches; can discover DSA on LAN | | | critical | |
| SAT-DEP-003 | Deployment | RAA install + enrollment | Published RAA release; enrollment key configured | 1. Download RAA<br>2. Install<br>3. Enroll with `X-Enrollment-Key`<br>4. Confirm heartbeat | Workstation registered; heartbeat shows Online | | | critical | |
| SAT-DEP-004 | Deployment Center | SHA-256 / signature / compatibility matrix display | Release with checksum + min version matrix | 1. Open `/deployment-center`<br>2. Inspect DSA, RAA, Wizard cards | Checksum, release notes, compatibility matrix visible | | | high | |
| SAT-DEP-005 | Deployment Center | Repair package download | Repair package attached to release | 1. Download repair package<br>2. Run on agent with broken state | Agent recovers without full reinstall | | | high | |
| SAT-DEP-006 | Deployment Center | Upgrade package preserves config | Prior agent version installed with identity | 1. Download upgrade<br>2. Install over existing<br>3. Inspect ProgramData | Identity/token retained; version bumped; config intact | | | high | |

### 8.2 Commissioning

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-COM-001 | Commissioning | Full lab chain Portal→DSA→EqPC→RAA→Analysis PC | Fresh department; equipment RA-enabled | 1. Enroll DSA<br>2. Pair Wizard; announce EqPC<br>3. Link RAA to equipment<br>4. Run node diagnostics | All nodes Online on Lab Infrastructure; config version set | | | critical | |
| SAT-COM-002 | DSA | Automatic discovery UDP/HTTP | DSA bound on LAN; firewall allows UDP 6010 | 1. Launch Wizard discover<br>2. Try HTTP then UDP | DSA listed with correct IP/port | | | critical | |
| SAT-COM-003 | DSA | Pairing + announce + config pack | `ManagementApiKey` set on DSA | 1. Issue pairing token<br>2. Announce EqPC<br>3. Pull config-pack<br>4. Validate registration | Registration validated; OTP not persisted in ConfigJson (H-02 fix) | | | critical | Validates C-01/H-02 |
| SAT-COM-004 | RAA | Equipment binding + software inventory | Enrolled RAA; equipment exists | 1. Link equipment via installer API<br>2. Trigger inventory scan | Equipment linked; inventory visible on portal | | | high | |
| SAT-COM-005 | Heartbeat | DSA equipment_pcs rollup in Portal heartbeat | EqPC reported status to DSA `:6001` | 1. Wait for DSA heartbeat<br>2. GET `/api/v1/lab/infrastructure/` | EqPC appears under DSA with live status (C-01 fix) | | | critical | |
| SAT-COM-006 | Diagnostics | Node diagnostics PASS/WARN/FAIL | Online node in fleet tree | 1. Open node detail<br>2. Run diagnostics from Lab UI | Professional report JSON; node-scoped; no fleet-wide side effects | | | high | H-11 partial |

### 8.3 Booking workflow and sync

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-BKG-001 | Booking Workflow | Internal user E2E booking→RA→complete | Commissioned lab; internal user | Create→approve→sample→raw→sync→RA session→Guac→results→S3→email | Booking Complete; emails sent; cleanup done | | | critical | |
| SAT-BKG-002 | Booking Workflow | Faculty E2E | Faculty account with dept scope | Same as BKG-001 with faculty login | Passes with faculty RBAC | | | high | |
| SAT-BKG-003 | Booking Workflow | External user E2E | Verified external org | Same as BKG-001 with external login | Passes charge/approval path | | | high | |
| SAT-BKG-004 | Booking Workflow | Project user E2E | Project wallet funded | Same as BKG-001 with project billing | Passes project billing | | | medium | |
| SAT-BKG-005 | Booking Workflow | Startup user E2E | Startup account | Same as BKG-001 with startup path | Passes startup path | | | medium | |
| SAT-BKG-006 | Synchronization | DSA raw data sync after sample accept | EqPC folders + share configured | 1. Drop raw file in instrument folder<br>2. Accept sample on portal<br>3. Observe DSA sync | Portal sees files; SyncLog success | | | critical | |

### 8.4 Remote Analysis

Detailed RA sync SAT: [`docs/sat/01-Detailed-Checklist.md`](../sat/01-Detailed-Checklist.md). Expected API sequences: [`docs/sat/03-Expected-API-Sequence.md`](../sat/03-Expected-API-Sequence.md).

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-RA-001 | Remote Analysis | Session create + reverse tunnel + Guacamole | Analysis PC Online; reverse tunnel gateway up | 1. Start RA session from booking<br>2. Open Guacamole URL | Desktop reachable within SLA; tunnel ACTIVE | | | critical | |
| SAT-RA-002 | Remote Analysis | Clipboard + file transfer | Active Guacamole session | 1. Copy text host→remote<br>2. Transfer file per policy | Both succeed per configured policy | | | high | |
| SAT-RA-003 | Remote Analysis | Timeout / extension / maintenance / queue | Policies configured on equipment | Exercise timeout, extension, maintenance window, queue wait | Expected UX + audit events | | | high | |
| SAT-RA-004 | Remote Analysis | Workspace cleanup + archive | Completed session | 1. End analysis<br>2. Wait cleanup task | Workspace cleaned/archived; workstation AVAILABLE | | | high | |
| SAT-RA-005 | Remote Analysis | Concurrent sessions multi-PC | ≥2 Analysis PCs Online | Start two sessions in parallel | No cross-talk; both sessions healthy | | | high | |
| SAT-RA-006 | Remote Analysis | Session recovery + no-show | Policies set | 1. Kill agent mid-session<br>2. Trigger no-show booking | Recovery/no-show handlers fire; audit logged | | | medium | |

### 8.5 DSA, configuration push, software inventory

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-DSA-001 | DSA | IP allocation + reservation | Soft IP pool; MAC known | Announce same EqPC twice | Preferred IP reused; reservation recorded | | | high | |
| SAT-DSA-002 | Configuration Push | Config push + ack + Applied status | Profile bound to DSA | 1. Apply template / bump config<br>2. DSA bootstrap<br>3. EqPC apply<br>4. POST ack | Ack Applied; dashboard shows Applied (C-02 fix) | | | critical | |
| SAT-DSA-003 | Configuration Push | Configuration rollback | ≥2 configuration versions | 1. Rollback profile<br>2. Re-bootstrap DSA | Previous snapshot restored; version bumped | | | high | C-02 fix |
| SAT-DSA-004 | DSA | Folder monitoring + repair + reconfigure | Validated EqPC | 1. Delete monitored folder<br>2. Invoke Repair<br>3. RefreshConfiguration | Folders restored; audit logged | | | high | H-06 partial |
| SAT-DSA-005 | Software Inventory | Required vs installed compliance matrix | Template with `required_software` | Open `/api/v1/lab/software/compliance/` or UI | Missing/Outdated flagged correctly | | | medium | |

### 8.6 Failure recovery

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-FAIL-001 | Failure Recovery | Stop DSA / disconnect LAN | Healthy lab | 1. Stop DSA 5 min<br>2. Disconnect LAN briefly<br>3. Restore | Alerts fire; auto reconnect; no data corruption | | | critical | |
| SAT-FAIL-002 | Failure Recovery | Stop RAA / delete ProgramData subset | Healthy RAA | 1. Stop service<br>2. Delete cache subset<br>3. Restart/reinstall | Re-enroll or recover per design | | | high | |
| SAT-FAIL-003 | Failure Recovery | Stop reverse tunnel / Guacamole | Active RA path | 1. Stop gateway<br>2. Restart | Sessions fail safely; recover after restart | | | critical | |
| SAT-FAIL-004 | Failure Recovery | Disk full / missing result folder | Writable volumes | 1. Fill disk or delete results folder | Alerts; diagnostics FAIL; repair path available | | | high | |
| SAT-FAIL-005 | Failure Recovery | Restart Portal + Database | Staging environment | Restart web + DB mid-sync | Agents reconnect; no orphan critical state | | | critical | |

Procedures: [`docs/sat/07-Recovery-Procedures.md`](../sat/07-Recovery-Procedures.md).

### 8.7 Fleet dashboard, repair, reporting, notifications

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-FLT-001 | Fleet Dashboard | Lab Infrastructure tree statuses | Mixed online/offline nodes | 1. Open `/laboratory-infrastructure`<br>2. Poll 30s | Correct status enums; auto-refresh (~20s) | | | critical | H-09 fix |
| SAT-FLT-002 | Fleet Dashboard | Health score + node detail fields | Heartbeat enriched | Open node detail drawer/page | CPU/RAM/disk/versions/tunnel status present | | | high | |
| SAT-FLT-003 | Repair | Repair / RestartAgent / RescanSoftware actions | Main Admin manage permission | Invoke each action from node detail | Command queued/sent; audit entry; restart applies correct branch (H-05 fix) | | | high | H-06 pending |
| SAT-FLT-004 | Reporting | Utilization CSV export | Usage data in period | GET `/api/v1/lab/reports/utilization/?format=csv` | CSV/JSON downloadable; correct columns | | | medium | |
| SAT-FLT-005 | Notifications | Critical alert email | Email stack configured | Trigger offline health detector | Alert record + email delivered | | | medium | SMS/WhatsApp deferred |

### 8.8 Security and RBAC

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-SEC-001 | Security | Pairing fail-closed without ManagementApiKey | DSA with unset key | POST `/api/pairing/issue` | 403 Forbidden (H-01 fix) | | | critical | |
| SAT-SEC-002 | Role Based Access | Lab + Test Dashboard Main Admin only | Non-admin user session | Navigate `/laboratory-infrastructure`, `/test-dashboard` | Denied or redirected | | | critical | |
| SAT-SEC-003 | Security | Agent auth + config integrity + credential storage | Enrolled agents | 1. Replay expired token<br>2. Inspect disk secrets | No plaintext secrets; invalid auth rejected | | | critical | |
| SAT-SEC-004 | Security | Status ingest not forgeable without pairing/mgmt key | DSA up | POST status from non-loopback without token | 401/403 (H-04 fix) | | | high | |

See [Security-Test-Plan.md](./Security-Test-Plan.md) for full matrix.

### 8.9 API, database, frontend

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-API-001 | API | Lab aggregate APIs authz + validation | OpenAPI / Swagger | Call with/without auth; malformed payloads | 401/403/400 as documented | | | high | |
| SAT-API-002 | API | Config ack idempotent upsert | DSA agent token | POST ack twice with same version | Single logical ack row; 201 | | | medium | |
| SAT-DB-001 | Database | Phase 1/2 migrations apply + rollback smoke | Staging DB backup | `migrate`; safe reverse smoke | No integrity errors | | | high | |
| SAT-DB-002 | Database | Indexes/FKs on lab_infrastructure tables | Migrations applied | Inspect schema / `\d` | Indexes present; cascades correct | | | medium | |
| SAT-FE-001 | Frontend | Lab Infrastructure UX states | Main Admin browser | Exercise loading, empty, error, filter states | Indicators and empty states correct | | | medium | |
| SAT-FE-002 | Frontend | Deployment Center + Test Dashboard responsive | Desktop + tablet widths | Resize viewport | Usable layout without horizontal scroll | | | low | |

### 8.10 Performance (cross-reference)

Executed under [Performance-Test-Plan.md](./Performance-Test-Plan.md).

| Test ID | Module | Feature | Preconditions | Test Steps | Expected Result | Actual Result | Status | Severity | Remarks |
|---------|--------|---------|---------------|------------|-----------------|---------------|--------|----------|---------|
| SAT-PERF-001 | Performance | Lab infrastructure API p95 | ≥50 nodes or simulated | Load test GET `/api/v1/lab/infrastructure/` | p95 < 2s staging baseline | | | medium | H-10 watch |
| SAT-PERF-002 | Performance | Heartbeat + concurrent sync | Multi-agent lab | Burst heartbeats + parallel syncs | No dropped critical updates | | | medium | |

---

## 9. Master feature-area checklist

Use this section to confirm no Phase 1/2 area is omitted before sign-off.

| Feature area | Primary test IDs | RA SAT reference | Covered |
|--------------|------------------|------------------|---------|
| Deployment Center + installers | SAT-DEP-* | — | ☐ |
| Equipment PC Wizard | SAT-DEP-002, SAT-COM-002/003 | — | ☐ |
| DSA discovery / pairing / announce | SAT-COM-002/003, SAT-SEC-001 | — | ☐ |
| DSA heartbeat + equipment_pcs rollup | SAT-COM-005 | — | ☐ |
| IP reservation (soft) | SAT-DSA-001 | — | ☐ |
| Equipment templates + config push | SAT-DSA-002 | — | ☐ |
| Configuration rollback | SAT-DSA-003 | — | ☐ |
| RAA enrollment + link | SAT-DEP-003, SAT-COM-004 | SAT-01 | ☐ |
| Reverse tunnel transport | SAT-RA-001, SAT-FAIL-003 | SAT + [`docs/ReverseTunnelArchitecture.md`](../ReverseTunnelArchitecture.md) | ☐ |
| Guacamole session | SAT-RA-001/002 | [`docs/sat/13-Guacamole-Session-SAT.md`](../sat/13-Guacamole-Session-SAT.md) | ☐ |
| Booking E2E (all personas) | SAT-BKG-* | SAT-05 | ☐ |
| Raw data sync (DSA) | SAT-BKG-006 | SAT-04 | ☐ |
| Workspace lifecycle + file sync | SAT-RA-004 | SAT-03, SAT-04 | ☐ |
| Maintenance mode + queue | SAT-RA-003 | — | ☐ |
| Software inventory / compliance | SAT-DSA-005, SAT-COM-004 | — | ☐ |
| Lab Infrastructure fleet tree | SAT-FLT-001/002 | — | ☐ |
| Diagnostics (node-scoped) | SAT-COM-006 | — | ☐ |
| Repair actions | SAT-FLT-003 | — | ☐ |
| Health detectors + alerts | SAT-FLT-005 | — | ☐ |
| Utilization reporting | SAT-FLT-004 | — | ☐ |
| Agent updates (discover/report) | SAT-SEC-003 | H-07/H-08 fix | ☐ |
| Security / RBAC | SAT-SEC-* | SAT-07 | ☐ |
| Failure recovery | SAT-FAIL-* | SAT-06 | ☐ |
| API contract + DB integrity | SAT-API-*, SAT-DB-* | SAT-09 | ☐ |
| Frontend UX | SAT-FE-* | — | ☐ |
| Performance baselines | SAT-PERF-* | SAT-08 | ☐ |

---

## 10. Entry criteria

- [ ] All Critical defects (C-*, H-01 … H-05, H-09, H-12, C-01, C-02, H-02, H-04, H-07, H-08) marked **Resolved** in [Production-Readiness-Report.md](./Production-Readiness-Report.md)
- [ ] Migrations applied through latest `lab_infrastructure`, `deployment`, `sync`, `remote_analysis` heads
- [ ] Published installer artifacts in Deployment Center with SHA-256
- [ ] Lab VLAN with at least one DSA, one EqPC, one Analysis PC
- [ ] Main Admin test account; persona accounts for UAT
- [ ] Test dashboard catalog seeded (`ensure_catalog`)

## 11. Exit criteria

SAT is **complete** only when:

1. Every catalog row is **PASS** or documented **N/A** with owner approval.
2. No open **Critical** or **High** defects without accepted mitigation.
3. Lab E2E paths SAT-COM-001, SAT-BKG-001, SAT-RA-001 signed with evidence.
4. [Final-Acceptance-Checklist.md](./Final-Acceptance-Checklist.md) fully checked.
5. [Production-Readiness-Report.md](./Production-Readiness-Report.md) signed **GO** or **Conditional GO** with residual risks listed.

---

## 12. Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| SAT lead | | | |
| Portal engineering | | | |
| Agent engineering (DSA/RAA) | | | |
| Security | | | |
| Operations | | | |
| Product owner | | | |
