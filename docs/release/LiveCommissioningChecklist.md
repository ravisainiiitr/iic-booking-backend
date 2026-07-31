# Live Commissioning Checklist — `1.0.0-RT-RC1`

**Environment:** Production Portal (AWS) + Gateway + Agent + Guacamole + IIT Analysis PC  
**Transport during this checklist:** start with `direct_rdp`; enable `reverse_tunnel` only for tunnel/desktop sections when approved.  
**CommissioningRunId:** ________________  
**Operator:** ________________ **Date (UTC):** ________________

For every row: mark **PASS** or **FAIL**, attach evidence (run id / log path / screenshot id), initials, date.

| # | Area | Validation step | PASS | FAIL | Evidence | Initials | Date |
|---|------|-----------------|------|------|----------|----------|------|
| 1 | Portal | Portal root HTTPS reachable | ☐ | ☐ | | | |
| 2 | Portal | `/api/v1/analysis/health/live/` = 200 | ☐ | ☐ | | | |
| 3 | Portal | Toolkit / Live Commissioning loads (manage user) | ☐ | ☐ | | | |
| 4 | Portal | Migration `0015` applied; tunnel tables exist | ☐ | ☐ | | | |
| 5 | Portal | `RA_TRANSPORT` / settings = expected mode for this run | ☐ | ☐ | | | |
| 6 | Gateway | Container Up / healthcheck pass | ☐ | ☐ | | | |
| 7 | Gateway | Admin health endpoint OK | ☐ | ☐ | | | |
| 8 | Gateway | Metrics endpoint OK | ☐ | ☐ | | | |
| 9 | Gateway | No crash-loop in gateway logs | ☐ | ☐ | | | |
| 10 | Agent | Service installed/running on Analysis PC | ☐ | ☐ | | | |
| 11 | Agent | Registration visible in Portal | ☐ | ☐ | | | |
| 12 | Heartbeat | Heartbeat age ≤ 90s (GREEN) | ☐ | ☐ | | | |
| 13 | Workspace | Workspace created for completed booking | ☐ | ☐ | | | |
| 14 | Workspace | RawData present / sync state OK | ☐ | ☐ | | | |
| 15 | Workspace | Input checksums match (if applicable) | ☐ | ☐ | | | |
| 16 | Tunnel | Tunnel requested (when reverse_tunnel on) | ☐ | ☐ | | | |
| 17 | Tunnel | Agent accepted JOIN_TUNNEL | ☐ | ☐ | | | |
| 18 | Tunnel | Tunnel Connected / ACTIVE | ☐ | ☐ | | | |
| 19 | Tunnel | No duplicate ACTIVE tunnels | ☐ | ☐ | | | |
| 20 | Guacamole | Guacamole + guacd healthy | ☐ | ☐ | | | |
| 21 | Guacamole | Connection uses adapter host:port (reverse_tunnel) or direct RDP | ☐ | ☐ | | | |
| 22 | Desktop | Browser desktop launches | ☐ | ☐ | | | |
| 23 | Desktop | Mouse / keyboard usable | ☐ | ☐ | | | |
| 24 | Desktop | Clipboard (per policy) | ☐ | ☐ | | | |
| 25 | Desktop | Window resize / reconnect | ☐ | ☐ | | | |
| 26 | Software | Analysis app operable (Origin / MATLAB / XRD as equipped) | ☐ | ☐ | | | |
| 27 | Results | Output saved on workstation | ☐ | ☐ | | | |
| 28 | Upload | Results uploaded / collect completed | ☐ | ☐ | | | |
| 29 | Upload | No duplicate uploads | ☐ | ☐ | | | |
| 30 | Cleanup | Cleanup started and finished | ☐ | ☐ | | | |
| 31 | Release | Workstation released | ☐ | ☐ | | | |
| 32 | Release | Next researcher can allocate | ☐ | ☐ | | | |
| 33 | Evidence | Evidence ZIP archived | ☐ | ☐ | | | |

## Sign-off

| Role | Name | PASS overall | Signature / date |
|------|------|--------------|------------------|
| Commissioning engineer | | ☐ | |
| Ops approver | | ☐ | |

**Overall:** PASS ☐ / FAIL ☐  

On FAIL: stop → Defect Workflow → minimal fix → re-run failed rows only after regression.
