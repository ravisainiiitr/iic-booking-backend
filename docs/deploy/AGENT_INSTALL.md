# Remote Analysis Agent — Install Notes (Windows Analysis PC)

The Agent is **not** deployed via Docker. Install on each Analysis PC.

## Prerequisites

- Windows 10/11 or Server with RDP enabled  
- Network: PC can reach Portal HTTPS; **guacd** can reach PC:3389  
- Service account for Guacamole RDP (stored in Portal `WorkstationRdpSecret`)  

## Steps

1. Install `RemoteAnalysisAgent` MSI/service build matching Portal RC tag.  
2. Configure `appsettings` / options:  
   - `PortalBaseUrl` = `https://<portal>/`  
   - Enrollment key matching `RA_AGENT_ENROLLMENT_KEY`  
   - `SessionWorkspaceRoot` (default under ProgramData)  
3. Start service; confirm registration.  
4. Portal Toolkit → Agent diagnostics: heartbeat age &lt; 90s.  
5. Configure RDP secret in Django admin for the workstation.  

## Paths

- State: `C:\ProgramData\RemoteAnalysisAgent\State\`  
- Logs: `C:\ProgramData\RemoteAnalysisAgent\Logs\`  
- Local health (optional): `http://127.0.0.1:5088/api/health`  

## Upgrade

Stop service → replace binaries → start → verify heartbeat.  
Portal rollback does not require Agent rollback unless API contract changed (RC1 freeze: no API changes).
