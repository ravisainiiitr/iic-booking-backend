# Phase RX — Remote Analysis Incident Fixes

| Doc | Status |
|-----|--------|
| [RX.1-PXRD-Queue-Stall-Assessment.md](./RX.1-PXRD-Queue-Stall-Assessment.md) | Assessment |
| [RX.1-PXRD-Queue-Stall-Fix-Report.md](./RX.1-PXRD-Queue-Stall-Fix-Report.md) | Fix + evidence |
| `ra-diag-*.txt` | Production diagnose dumps |

## Current posture (RX.1)

| Item | Status |
|------|--------|
| New RAA `DESKTOP-CSMH6BU` for PXRD [A] | Discovered + pooled |
| Root cause (empty InstalledSoftware) | **Fixed** + backfilled |
| Misleading Scheduled Maintenance UX | **Fixed** (deployed) |
| RESERVED soft-online expire bug | **Fixed** (deployed v2.5.7) |
| Live allocation | **AWAITING_CHECKIN** on DESKTOP-CSMH6BU |
| Guacamole launch | Needs user check-in |
| Agent heartbeat on new RAA | Still null — ops follow-up |
