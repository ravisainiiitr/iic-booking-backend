# RAA Automation + Deployment Center

## Remote Analysis Agent

### Enrollment key

```json
"Agent": {
  "PortalUrl": "https://portal.example.edu",
  "EnrollmentKey": "shared-secret",
  "LinkEquipmentId": 42
}
```

When `EnrollmentKey` is set, register and link requests send `X-Enrollment-Key`.

Portal enforces the key only when `RA_AGENT_ENROLLMENT_KEY` is configured; register stays open if unset.

### Equipment link

After registration, if `LinkEquipmentId` or `EquipmentId` is set:

`POST /api/v1/analysis/installer/link/` with `{ equipment_id, agent_id, workstation_id? }`.

### Diagnostics

- Post-register: `PostInstallDiagnostics` logs PASS/WARNING/FAIL
- HTTP: `GET /api/diagnostics/commissioning`

Checks: Portal reachable, registered, token present, fingerprint, enrollment key configured.

### Publish Setup EXE

```bash
python manage.py publish_ra_installer path/to/RemoteAnalysisAgentSetup.exe --release-version 1.0.0
```

## Deployment Center

Main Admin page `/deployment-center` aggregates DSA, RA, and Equipment PC Wizard releases (version, SHA-256, signature status, ticket download, previous versions, guides).
