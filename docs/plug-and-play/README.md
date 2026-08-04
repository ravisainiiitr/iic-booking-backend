# Plug-and-Play Laboratory Infrastructure — Phase 1

## Architecture (preserved)

```
Portal → DSA → Equipment PCs
Portal → RAA → Analysis PCs
```

Do not invert Portal as source of truth for bookings/sync. DSA remains the department control plane for Equipment PCs; Portal remains the RA control plane for Analysis PCs.

## Deployment Center

- **UI:** `/deployment-center` (Main Admin)
- **API:** `GET /api/v1/deployment/center/`
- **Wizard releases:** `EquipmentPcWizardRelease` under `iic_booking.deployment`
- **Publish commands:**
  - `python manage.py publish_dsa_installer <path> --release-version 1.0.0`
  - `python manage.py publish_ra_installer <path> --release-version 1.0.0`
  - `python manage.py publish_equipment_wizard <path> --release-version 1.0.0`

Ticket downloads include SHA-256 when published. Product ticket paths:

| Product | Ticket download |
|---------|-----------------|
| DSA | `/api/v1/sync/installer/releases/download/ticket/<token>/` |
| RA | `/api/v1/analysis/installer/releases/download/ticket/<token>/` |
| Equipment PC Wizard | `/api/v1/deployment/wizard/download/<token>/` |

## Equipment PC Configuration Wizard

Project: `DepartmentSyncAgent/Backend/src/EquipmentPcConfigurationWizard`

Technician flow (~3–5 min):

1. Discover DSA (preferred HTTP `http://192.168.1.100:6001/api/discovery/info`, then UDP `DSA-DISCOVER` / `DSA-ADVERTISE` on port 6010)
2. Issue pairing token (`POST /api/pairing/issue`)
3. Select LAN adapter + equipment
4. Announce PC (`POST /api/equipment-pcs/announce`)
5. Pull config pack and apply (folders; user/share/firewall stubs behind elevation)
6. Validate and report PASS / WARN / FAIL

### DSA Discovery Protocol

| Channel | Detail |
|---------|--------|
| HTTP | `GET /api/discovery/info` |
| UDP | Port 6010 — payload `DSA-DISCOVER` → `DSA-ADVERTISE{...json}` |
| Auth | Header `X-Pairing-Token` (15 min TTL) |

## IP Reservation Strategy

**Phase 1 (soft):**

- DSA SQLite tables `IpReservation` + `EquipmentPcRegistration`
- On announce: reuse preferred IP for known MAC; default `network_mode=dhcp`
- Register observed IP; do **not** force campus-wide static
- Static intent only when template/policy sets `network_mode=static` (wizard writes intent marker under `%ProgramData%\DepartmentSyncAgent\EquipmentPcWizard\`)

**Phase 2:** full allocator (pool, gateway, DNS) + conflict scan + netsh apply behind admin toggle.

## Equipment Templates + Config Push

- Model: `EquipmentSyncTemplate` (Portal sync app)
- Apply template → copy fields onto `EquipmentSyncProfile` → bump `configuration_version` → set `bootstrap_required` on assigned agents
- Existing heartbeat / bootstrap path becomes **Configuration Push**
- Bootstrap document extended with: `network_mode`, `windows_account_policy`, `folder_layout`, `firewall_profile`, `retry_policy`, `required_software`, `health_thresholds`, `template_code`

Admin: Django Admin → Equipment Sync Templates → Apply form.

## RAA Automation

- `Agent:EnrollmentKey` → header `X-Enrollment-Key` on register/link
- Optional `LinkEquipmentId` / `EquipmentId` → `POST /api/v1/analysis/installer/link/` after register
- Diagnostics: `GET /api/diagnostics/commissioning` and post-register logging via `PostInstallDiagnostics`

## Security (Phase 1 minimum)

- No plaintext password files; one-time passwords intended for Windows Credential Manager
- Wizard↔DSA requires pairing token
- Signed installers + SHA-256 displayed in Deployment Center

## Related docs

- [EquipmentPcConfigurationWizard.md](./EquipmentPcConfigurationWizard.md)
- [DsaDiscoveryProtocol.md](./DsaDiscoveryProtocol.md)
- [IpReservationStrategy.md](./IpReservationStrategy.md)
- [EquipmentTemplatesConfigPush.md](./EquipmentTemplatesConfigPush.md)
- [RaaAutomationDeploymentCenter.md](./RaaAutomationDeploymentCenter.md)
- [Phase1TestingReport.md](./Phase1TestingReport.md)
