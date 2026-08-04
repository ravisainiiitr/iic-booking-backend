# Equipment PC Configuration Wizard

WPF companion in `DepartmentSyncAgent/Backend/src/EquipmentPcConfigurationWizard`.

Talks to **local DSA** (`:6001` + discovery), not Portal enrollment secrets for DSA↔Portal.

## Run

```powershell
# Terminal 1
dotnet run --project D:\IIC_NEW\DepartmentSyncAgent\Backend\src\DepartmentSyncAgent.Api

# Terminal 2 (elevated recommended for apply steps)
dotnet run --project D:\IIC_NEW\DepartmentSyncAgent\Backend\src\EquipmentPcConfigurationWizard
```

## Steps

1. **Discover** — HTTP preferred IP, then UDP multi-DSA picker
2. **Select NIC** — LAN adapter (MAC used for soft IP reservation)
3. **Select Equipment** — from DSA `GET /api/equipment/list`
4. **Apply** — folders + stubs for user/share/firewall; static IP intent only if `network_mode=static`
5. **Validate** — folder existence + share stub; submit to DSA

## Credentials

One-time password from config pack must be stored in Windows Credential Manager / LSA — never written as plaintext files.

## Download

Published via Portal Deployment Center (`publish_equipment_wizard`) when an EXE is produced for technicians.
