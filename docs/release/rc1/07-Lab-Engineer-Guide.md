# Remote Analysis — Lab Engineer Guide (RC1)

## Responsibilities

- Install/configure Remote Analysis Agent on Analysis PCs  
- Ensure RDP reachable **from guacd** (not from Portal)  
- Disk space under session workspace root  
- Windows account used for Guacamole RDP (`WorkstationRdpSecret`)  
- First-workstation commissioning (Phase 2 live runbook)  

## Agent install checklist

- [ ] Service installed and starts on boot  
- [ ] `PortalBaseUrl` points at production HTTPS Portal  
- [ ] Enrollment completes; agent token stored securely  
- [ ] Heartbeat visible in Toolkit (&lt; 90s age)  
- [ ] `PREPARE` / `CLEAN` succeed for a test workspace  
- [ ] Logs under `C:\ProgramData\RemoteAnalysisAgent\Logs\`  

## Network

| Path | Required |
|------|----------|
| Agent → Portal HTTPS | Yes |
| guacd → PC RDP 3389 | Yes (desktop) |
| Portal → PC RDP | Not required |
| Browser → Guacamole HTTPS | Yes (desktop) |

## Commissioning

Follow [RemoteAnalysisLiveCommissioning.md](../../RemoteAnalysisLiveCommissioning.md).  
Use observability Run ID / evidence ZIP for defect triage.  
Do not change production workflows during live commissioning.

## Guacamole desktop

After sync path is green, validate SAT-11 cases needed for your site (mock vs live).
