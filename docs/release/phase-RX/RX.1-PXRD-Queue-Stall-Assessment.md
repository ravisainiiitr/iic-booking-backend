# RX.1 — PXRD [A] Queue Stall Assessment

**Booking:** `IICPXRD [A]202600040` (pk 366)  
**Equipment:** Powder X-Ray Diffractometer (PXRD) [A] (id 1, dept 33)  
**New RAA:** `DESKTOP-CSMH6BU` (`8715e5a2-296d-41f1-a1f9-e2f5efc80612`)

## Symptom

Analysis Workspace stuck on **Waiting in queue** with:

- Queue 1 of 1, estimated wait 0 min
- Workspace READY / Environment Ready tiles
- Banner: **No compatible Analysis Workstation… Reason: Scheduled Maintenance**

## Flow stop point

```
Booking → reservation QUEUED → scheduler allocate
  → candidates evaluated
  → REJECT all: Missing required software: Notepad
  → stay QUEUED
  → experience.queue uses MaintenanceService fallback → "Scheduled Maintenance"
```

## Classification

| Area | Status |
|------|--------|
| New RAA registered + in EquipmentAnalysisPool | **PASS** |
| Department match (33) | **PASS** |
| Status AVAILABLE / enabled | **PASS** |
| Heartbeat | **FAIL** (`last_heartbeat=None`) — soft-online via token still allowed |
| InstalledSoftware inventory | **FAIL** (empty before fix) |
| Required software for PXRD [A] | `['Notepad']` (catalog slug `notepad` — intentional test software) |
| Scheduler rejection | Missing required software: Notepad |
| “Scheduled Maintenance” | **UX DEFECT** — generic fallback when no matching AVAILABLE software inventory (R8.5) |

## Notepad note

Notepad is the **configured** catalog software for PXRD [A] in production (test/lab mapping). It is not a UI glitch. Discovery keywords prefer `notepad++`; catalog entry “Notepad” was not present on the agent inventory until seeded.
