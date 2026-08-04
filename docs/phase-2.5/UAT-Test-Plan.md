# UAT Test Plan — Phase 2.5

**Date:** 2026-08-04  
**Scope:** User acceptance testing for Phase 1 Plug-and-Play + Phase 2 Enterprise Lifecycle  
**Goal:** Validate that real personas can complete their day-to-day workflows without workarounds.

UAT complements SAT ([SAT-Master-Test-Plan.md](./SAT-Master-Test-Plan.md)) by focusing on business outcomes, UX clarity, and role-appropriate access — not low-level API contracts.

---

## 1. Personas

| Persona | Typical role | Primary surfaces |
|---------|--------------|------------------|
| **Main Admin** | Institute IT / platform owner | Deployment Center, Lab Infrastructure, Test Dashboard, Django Admin templates |
| **Dept Admin** | Department instrument manager | Equipment config, bookings approval, dept reports |
| **Faculty** | Internal researcher | Booking creation, sample submission, RA session |
| **External** | External org user | Registration, booking, billing, RA (if entitled) |
| **Operator** | Lab technician / instrument operator | Sample accept/reject, raw data handling, on-floor troubleshooting |

---

## 2. Environment and data

- **Portal:** Staging URL with production-like data volume (≥1 department, ≥2 instruments, ≥1 Analysis PC).
- **Agents:** Live DSA, at least one commissioned EqPC, at least one Online Analysis PC.
- **Accounts:** One account per persona; passwords rotated for UAT only.
- **Evidence:** Screenshot, booking ID, timestamp, and brief operator notes per scenario.

---

## 3. UAT scenarios by persona

### 3.1 Main Admin

| ID | Scenario | Steps | Acceptance criteria | SAT cross-ref | Status |
|----|----------|-------|---------------------|---------------|--------|
| UAT-MA-01 | Download and verify DSA installer | Open Deployment Center → download DSA → compare SHA-256 | Checksum matches published value; install succeeds | SAT-DEP-001 | |
| UAT-MA-02 | Download Wizard and RAA | Download both artifacts; note compatibility matrix | Matrix readable; tickets expire appropriately | SAT-DEP-002/003 | |
| UAT-MA-03 | Fleet visibility | Open Lab Infrastructure; locate DSA → EqPC → Analysis PC chain | Tree matches physical lab; statuses update within 30s | SAT-FLT-001 | |
| UAT-MA-04 | Push configuration template | Apply template to dept profile; observe bootstrap + ack | Dashboard shows Applied; EqPC receives new folders/policy | SAT-DSA-002 | |
| UAT-MA-05 | Rollback configuration | Rollback to prior version; confirm DSA re-bootstrap | Previous config restored; version incremented | SAT-DSA-003 | |
| UAT-MA-06 | Node diagnostics | Run diagnostics on DSA and Analysis PC | PASS/WARN/FAIL report readable; no unintended fleet actions | SAT-COM-006 | |
| UAT-MA-07 | Repair action | Trigger RestartAgent on offline Analysis PC | Command sent; PC returns Online; audit visible | SAT-FLT-003 | |
| UAT-MA-08 | Utilization export | Export utilization CSV for last 30 days | CSV opens in Excel; columns match booking usage | SAT-FLT-004 | |
| UAT-MA-09 | Test dashboard oversight | Open `/test-dashboard`; review module health | Module summary matches SAT catalog; can start run | SAT-FE-002 | |
| UAT-MA-10 | Security denial for admin routes | Log in as Faculty; attempt Lab Infrastructure URL | Access denied or redirect to authorized home | SAT-SEC-002 | |

### 3.2 Dept Admin

| ID | Scenario | Steps | Acceptance criteria | SAT cross-ref | Status |
|----|----------|-------|---------------------|---------------|--------|
| UAT-DA-01 | Configure equipment for RA | Set session duration, RAW/RESULTS dirs, required software | Fields persist; visible on equipment detail | SAT-COM-004 | |
| UAT-DA-02 | Approve internal booking | Review pending booking → approve | User notified; booking moves to approved state | SAT-BKG-001 | |
| UAT-DA-03 | Approve external booking | Review external booking with charges | Charge path correct; approval audit logged | SAT-BKG-003 | |
| UAT-DA-04 | Maintenance window | Schedule maintenance on Analysis PC | PC excluded from allocation; queue message clear | SAT-RA-003 | |
| UAT-DA-05 | Software compliance review | Open software compliance for department | Missing software highlighted for non-compliant PCs | SAT-DSA-005 | |
| UAT-DA-06 | Cannot access Deployment Center | Navigate to `/deployment-center` | Denied (Main Admin only) | SAT-SEC-002 | |

### 3.3 Faculty

| ID | Scenario | Steps | Acceptance criteria | SAT cross-ref | Status |
|----|----------|-------|---------------------|---------------|--------|
| UAT-FA-01 | Create booking | Select instrument → choose slots → submit | Booking created; confirmation email | SAT-BKG-002 | |
| UAT-FA-02 | Submit sample details | Complete sample form post-approval | Sample visible to operator queue | SAT-BKG-002 | |
| UAT-FA-03 | Remote analysis session | When slot active → Start Analysis → Guacamole | Desktop loads; session timer visible | SAT-RA-001 | |
| UAT-FA-04 | Extend session | Request extension within policy | Extension granted or denied with clear reason | SAT-RA-003 | |
| UAT-FA-05 | Download results | After completion → download from booking detail | Files match uploaded results; checksum integrity | SAT-BKG-002 | |
| UAT-FA-06 | View booking status during sync | Observe workspace phases on booking page | Phases match backend (Preparing → InputReady → …) | SAT-BKG-001 | |

### 3.4 External user

| ID | Scenario | Steps | Acceptance criteria | SAT cross-ref | Status |
|----|----------|-------|---------------------|---------------|--------|
| UAT-EX-01 | Org verification gate | Register / log in as external | Cannot book until org verified | SAT-BKG-003 | |
| UAT-EX-02 | Create paid booking | Book instrument with external tariff | Charge calculated; payment/wallet path clear | SAT-BKG-003 | |
| UAT-EX-03 | RA entitlement | Start RA if equipment allows external RA | Same UX as faculty where entitled | SAT-RA-001 | |
| UAT-EX-04 | Completion notification | Complete booking end-to-end | Completion email received with result links | SAT-BKG-003 | |
| UAT-EX-05 | No admin surfaces | Attempt lab/deployment URLs | All denied | SAT-SEC-002 | |

### 3.5 Operator

| ID | Scenario | Steps | Acceptance criteria | SAT cross-ref | Status |
|----|----------|-------|---------------------|---------------|--------|
| UAT-OP-01 | Accept sample | Open operator queue → accept sample | Booking advances; folders prepared on EqPC | SAT-BKG-006 | |
| UAT-OP-02 | Raw data appears on portal | Drop file in instrument raw folder after accept | Portal shows file within sync SLA | SAT-BKG-006 | |
| UAT-OP-03 | Reject sample with reason | Reject with comment | User notified; booking state correct | SAT-BKG-001 | |
| UAT-OP-04 | On-floor EqPC re-run Wizard | Re-run wizard repair path on same MAC | No duplicate registration; repair succeeds | SAT-DSA-004 | |
| UAT-OP-05 | Observe offline alert | Stop DSA briefly | Alert visible to Main Admin; recovers on restart | SAT-FAIL-001 | |
| UAT-OP-06 | Cannot push config or repair fleet | Attempt lab repair API/UI | Denied unless granted operator+admin role | SAT-SEC-002 | |

---

## 4. Cross-persona workflow (end-to-end narrative)

**Scenario UAT-E2E-01 — Full booking with RA (happy path)**

| Step | Actor | Action | Expected outcome |
|------|-------|--------|------------------|
| 1 | Main Admin | Commission lab (DSA, EqPC, Analysis PC) | Fleet tree all Online |
| 2 | Faculty | Create and submit booking | Pending approval |
| 3 | Dept Admin | Approve booking | User notified |
| 4 | Faculty | Submit sample metadata | Operator queue updated |
| 5 | Operator | Accept sample | Raw folder ready |
| 6 | System | DSA syncs raw to portal | Files visible on booking |
| 7 | Faculty | Start RA at slot time | Guacamole session active |
| 8 | Faculty | Complete analysis / End Analysis | Workspace cleanup scheduled |
| 9 | System | Results to S3 + email | Booking Complete |
| 10 | Main Admin | Verify audit + utilization | Events logged; usage counted |

**Scenario UAT-E2E-02 — External paid booking**

Same as E2E-01 with External user at steps 2–9; Dept Admin validates charge at step 3.

**Scenario UAT-E2E-03 — Maintenance blocks allocation**

Dept Admin schedules maintenance → Faculty attempts RA → clear queue/maintenance message → after window closes, RA succeeds.

---

## 5. Usability acceptance criteria

| Criterion | Pass threshold |
|-----------|----------------|
| Task completion without admin intervention | ≥95% of scripted UAT steps |
| Error messages | Actionable (state what failed and next step) |
| Role leakage | Zero unauthorized access to Deployment Center, Lab Infrastructure, Test Dashboard |
| Time to commission new EqPC (technician) | ≤15 minutes including wizard |
| Time to start RA from booking page | ≤2 minutes after slot open (excluding queue wait) |

---

## 6. Defect severity for UAT

| Severity | Definition | Blocks UAT sign-off? |
|----------|------------|----------------------|
| UAT-1 | Persona cannot complete primary workflow | Yes |
| UAT-2 | Workaround exists but unsafe or confusing | Yes, unless accepted |
| UAT-3 | Cosmetic / copy issue | No |

Link UAT defects to engineering IDs (C-*, H-*) when applicable.

---

## 7. Entry / exit criteria

**Entry**

- [ ] SAT Critical-path fixes deployed to staging
- [ ] Persona accounts provisioned
- [ ] Lab hardware available for operator and technician steps

**Exit**

- [ ] All UAT-1 scenarios PASS
- [ ] UAT-2 items documented with owner and target date
- [ ] Persona sign-off table completed below

---

## 8. Sign-off

| Persona representative | Scenarios reviewed | Pass / Fail | Date | Notes |
|------------------------|-------------------|-------------|------|-------|
| Main Admin | UAT-MA-* | | | |
| Dept Admin | UAT-DA-* | | | |
| Faculty | UAT-FA-* | | | |
| External | UAT-EX-* | | | |
| Operator | UAT-OP-* | | | |
| UAT lead | UAT-E2E-* | | | |
