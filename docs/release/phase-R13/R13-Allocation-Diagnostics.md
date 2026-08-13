# R13 — Allocation Diagnostics (FE-SEM / LabVIEW)

**Date:** 2026-08-13  
**Booking:** `IICAPREO202600001` (booking_id **385**, COMPLETED)  
**Equipment:** APREO — Field Emission Scanning Electron Microscope (FE-SEM)

## Root cause (PASS — diagnosed with production evidence)

| Observation | Fact |
|-------------|------|
| Required software | `NI LabVIEW Run-Time Engine 2010 SP1` |
| Installed = 1 | `InstalledSoftware` on **DESKTOP-CSMH6BU** (`allocation_enabled=True`) |
| Online = 0 | That workstation `status=OFFLINE`, **`last_heartbeat=None`** (never heartbeating) |
| Available = 0 | No matching PC with fresh heartbeat + AVAILABLE/ONLINE |
| Live RAA on RAVI | Online (`hb_age≈29s`, status RESERVED) but inventory shows **only Altium Designer 26** — **no LabVIEW** |
| Queue | Reservation `QUEUED`, no workstation assigned |

**Conclusion:** The allocation engine is **correct** not to allocate. The portal was wrong only in **wording** (“No compatible Analysis PC”) when a compatible PC **exists** but is offline / has never sent a heartbeat.

This is **not** a false offline on RAVI. RAVI is healthy; it simply does **not** have the required LabVIEW package installed (inventory published 1 title).

## Ops remediation (not a code bypass)

1. Start / re-register RAA on **DESKTOP-CSMH6BU** so heartbeats flow, **or**
2. Install LabVIEW Run-Time Engine 2010 SP1 on an online Analysis PC (e.g. RAVI) and wait for inventory sync, **or**
3. Remap FE-SEM equipment software to software that exists on online PCs.

When the matching PC heartbeats, existing queue should allocate automatically (no manual DB assign).

## Code fixes (this release)

- Experience pool counts use **heartbeat freshness** for Online/Available.
- Offline queue title becomes: **“The compatible Analysis PC is currently offline”** with hostname + heartbeat detail.
- Payload includes `environments.offline_pcs[]` for UI diagnostics.
- Default CTA label: **Open Analysis Workspace** (not Analyze Data).
