# Remote Analysis — Operator Guide (RC1)

## Day-to-day

1. Check Toolkit overview (online/offline workstations, failed workspaces)  
2. Check Guacamole tab (status `ok`, latency, active sessions)  
3. Triage open alerts / stuck sync phases  
4. Assist users who cannot see **Launch Remote Analysis**  

## User launch path

1. Booking eligible (experiment complete / equipment flag / analysis window)  
2. Open `/api/v1/bookings/{id}/analysis/desktop/?view=html`  
3. **Launch Remote Analysis** → Portal token → Guacamole redirect  
4. On end: user disconnect or operator terminate  

## Common operator actions

| Action | How |
|--------|-----|
| Force disconnect | `POST /api/v1/analysis/session/{id}/terminate/` |
| Re-run sync commissioning | Commissioning console (no Guacamole) |
| Collect evidence after failure | Toolkit runs → evidence ZIP |
| Check agent | Toolkit → Agent diagnostics |

## Escalation

- Agent offline → Lab Engineer (PC/network/service)  
- Guacamole unreachable → Administrator (gateway/TLS)  
- Data integrity / checksum fail → Lab Engineer + Administrator (storage)

Runbook: [RemoteAnalysisGuacamoleRunbook.md](../../RemoteAnalysisGuacamoleRunbook.md)
