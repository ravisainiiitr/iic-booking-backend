# Phase 1 Testing Report — Plug-and-Play Lab Platform

**Date:** 2026-08-03  
**Scope:** Phase 1 deliverables only  
**Commits:** Deferred until this gate passes (per plan)

## Gate checklist

| Test | Pass criteria | Status | Notes |
|------|----------------|--------|-------|
| Discover DSA on preferred IP | Wizard connects without manual IP entry | PENDING | Requires DSA bound on LAN / preferred IP |
| Discover via broadcast | Multi-DSA picker works | PENDING | UDP 6010; firewall may block |
| Equipment bind + config pack | User/folder/share created; sync path works | PARTIAL | Folders created; user/share/firewall still elevation stubs |
| Re-run wizard | Repair path; no duplicate DSA assignments | PENDING | Announce upserts by MAC/MachineGuid |
| RAA install + link | One AnalysisWorkstation; fingerprint reconnect after state wipe | PENDING | Code ready; live Analysis PC redeploy needed |
| Deployment Center download | Ticket download + checksum match | PENDING | APIs + UI added; needs published artifact |
| Image/recreate Portal | Installer APIs survive docker rebuild | PENDING | Prefer image rebuild (not `docker cp`) |

## Implementation status

| Deliverable | Status |
|-------------|--------|
| Deployment Center UI + aggregate API + Wizard release model | DONE |
| DSA discovery + announce + config-pack APIs | DONE |
| Equipment PC Wizard MVP | DONE (MVP stubs for elevated Windows ops) |
| EquipmentSyncTemplate + config push | DONE |
| RAA enrollment key + link + diagnostics | DONE |
| Soft IP reservation + optional static intent | DONE |
| Docs | DONE |

## Build smoke (local)

```powershell
dotnet build D:\IIC_NEW\DepartmentSyncAgent\Backend\DepartmentSyncAgent.slnx
dotnet build D:\IIC_NEW\RemoteAnalysis.Agent\src\RemoteAnalysis.Agent\RemoteAnalysis.Agent.csproj -c Release
```

Portal migrations to apply:

- `deployment.0001_equipment_pc_wizard_release`
- `sync.0017_equipment_sync_template`

## Security notes verified in design

- Pairing token required for Wizard↔DSA data plane
- No plaintext password files in Wizard apply path
- SHA-256 surfaced in Deployment Center

## Sign-off

- [ ] Lab technician Equipment PC flow (live LAN)
- [ ] Lab technician Analysis PC flow (live)
- [ ] Main Admin Deployment Center download + SHA verify
- [ ] Approve commits after gate

**Overall Phase 1 gate:** NOT PASSED (pending live E2E)
