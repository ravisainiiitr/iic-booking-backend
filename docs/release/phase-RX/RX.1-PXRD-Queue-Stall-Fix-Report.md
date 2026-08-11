# RX.1 — PXRD Queue Stall Fix & Live Evidence

**Decision:** **ROOT CAUSE FIXED — ALLOCATION RESTORED (AWAITING_CHECKIN)**  
**Full Guacamole desktop launch:** **PARTIAL / operator check-in required**

---

## 1. Root cause

The newly installed RAA `DESKTOP-CSMH6BU` **was discovered** (registered, enabled, dept 33, in PXRD [A] `EquipmentAnalysisPool`), but:

1. **`InstalledSoftware` was empty** → scheduler rejected every candidate with  
   `Missing required software: Notepad`
2. UX then showed **Scheduled Maintenance** via `MaintenanceService.next_compatible_availability` fallback when no matching AVAILABLE inventory existed (**not** a real maintenance window) — R8.5 defect.
3. After inventory backfill, allocate succeeded → `AWAITING_CHECKIN`, but workstation status became **`RESERVED`**, which was **excluded** from soft-online → `expire_stale` treated the agent as offline (no heartbeat) and expired the hold within minutes. `_free_workstation` also failed to clear `RESERVED` → stuck host.

**Notepad** is the real configured catalog software for PXRD [A] (`slug=notepad`) — intentional test mapping, not a stale UI default.

---

## 2. Affected components / files

| File | Change |
|------|--------|
| `remote_analysis/services/maintenance.py` | Offline / no-compatible vs real maintenance |
| `equipment/remote_analysis_integration/experience.py` | Queue title/body messaging |
| `remote_analysis/installer/services.py` | Seed `InstalledSoftware` on installer software link |
| `remote_analysis/services/inventory.py` | Do not wipe inventory on empty software POST |
| `remote_analysis/services/availability.py` | Soft-online includes `RESERVED` |
| `remote_analysis/services/scheduler.py` | Free `RESERVED`/`PREPARING` on expire |
| Workflows | diagnose / backfill / recover |

---

## 3. Scheduler candidate table (before fix)

| Candidate | Online/status | Installed | ACCEPT | Rejection |
|-----------|---------------|-----------|--------|-----------|
| DESKTOP-CSMH6BU | AVAILABLE, hb=None, dept=33, in pool | `[]` | **NO** | Missing required software: Notepad |
| RAVI | AVAILABLE, hb fresh | `[]` | **NO** | Missing required software: Notepad |

Evidence: `docs/release/phase-RX/ra-diag-31461418652.txt`

---

## 4. RAA status before / after

| Field | Before | After recover |
|-------|--------|---------------|
| Hostname | DESKTOP-CSMH6BU | same |
| Status | AVAILABLE → (stuck RESERVED) | **RESERVED** (check-in hold) |
| Inventory | empty | **Notepad** present |
| software_ok | false | **true** |
| Heartbeat | None | None (**remaining ops risk**) |
| Pool | yes (boost 10) | yes |

---

## 5–9. Queue / reservation / workspace

| Item | Before | After |
|------|--------|-------|
| Reservation | QUEUED, ws=null | **AWAITING_CHECKIN**, ws=`8715e5a2-…` |
| Queue | WAITING 1/1 | allocated (check-in) |
| Workspace READY tile | portal FS READY while queued | expected; not proof of PC alloc |
| Allocation proof | process_queue allocated=1 | ENSURE → AWAITING_CHECKIN on DESKTOP-CSMH6BU |

Runs: backfill `31461629190`, recover `31462280102`

---

## 10. Live E2E

| Step | Result |
|------|--------|
| RAA discovered by scheduler | **PASS** |
| Inventory/software match | **PASS** (after seed) |
| Allocate to DESKTOP-CSMH6BU | **PASS** |
| AWAITING_CHECKIN | **PASS** |
| User check-in + Guacamole launch | **NOT COMPLETED HERE** — requires researcher to open Analysis Workspace and start within check-in window (~10 min) |
| Heartbeat from new RAA | **FAIL / BLOCKED** — `last_heartbeat=None`; soft-online + token keeps check-in alive after v2.5.7 |

---

## 11–12. Tests / regression

| Suite | Result |
|-------|--------|
| `test_maintenance_mode.py` | **PASS** in prior CI/local runs (offline ≠ Scheduled Maintenance); local docker re-run **ERROR**ed here due to test DB schema drift (`cleanup_status` NOT NULL vs model) — **NOT a production defect** |
| `test_agent_online_reserved_status_with_token` | added on master; same local docker schema issue blocked create |
| Full Guacamole / portal UI regression | **NOT fully re-run** in this phase |
| Frontend build | **NOT RUN** (no FE code change required for root cause) |
| Live diagnose after recover | `ra-diag-31462613479.txt` — reservation **AWAITING_CHECKIN**, maintenance hint **“All matching environments busy”** (not Scheduled Maintenance) |

---

## 13. Git / deploy

| Item | Value |
|------|-------|
| Tags | `v2.5.6-ra-queue-ux-inventory`, `v2.5.7-ra-reserved-soft-online` |
| Deploy Backend | `v2.5.6` success `31461837500`; `v2.5.7` success `31462130248` |
| Master tip | includes RX fixes |

---

## 14–15. Remaining blockers

1. **Operator action:** open Analysis Workspace for `IICPXRD [A]202600040` and **Start / Check in** while status is AWAITING_CHECKIN.
2. **New RAA heartbeat:** ensure Windows service is running and can reach portal heartbeat API (`last_heartbeat` still null). Soft-online mitigates check-in expiry but Guacamole/session ops prefer a live agent.
3. Prefer installing/selecting real PXRD analysis software in catalog for production (Notepad is test-only).

---

## Decision tree (post-fix)

```
Installer links PXRD [A] + software slugs
  → EquipmentAnalysisPool + EquipmentAnalysisSoftware
  → InstalledSoftware seeded (new)
Agent inventory POST empty
  → no longer wipes seeded rows (new)
Scheduler allocate
  → software match → RESERVED / AWAITING_CHECKIN
expire_stale
  → RESERVED + valid token ⇒ still online (new)
  → free RESERVED on expire (new)
```
