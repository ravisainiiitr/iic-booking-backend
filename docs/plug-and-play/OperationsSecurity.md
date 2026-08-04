# Installation / Commissioning / Diagnostics / Troubleshooting / Security

## Installation

1. Main Admin opens **Deployment Center** and downloads the correct installer (DSA / RAA / Equipment PC Wizard).
2. Verify SHA-256 against the release metadata.
3. Run elevated on the target PC.

### Equipment PC

DSA must already be enrolled with Portal. Wizard discovers DSA, pairs, binds equipment, applies config pack.

### Analysis PC

RAA Setup/Agent registers with Portal (optional enrollment key), links equipment, runs commissioning diagnostics.

## Commissioning

Use the same RAG-style report shape:

- Network / Firewall / Folder / Share / Credentials / Portal or DSA / Sync or Heartbeat / Version

Overall: Healthy | Warnings | Fail + recommendations.

RAA: `GET /api/diagnostics/commissioning`  
Equipment Wizard: validation submit to DSA after local checks.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Wizard cannot discover DSA | Loopback-only bind / firewall / wrong preferred IP | Enable LAN bind; allow UDP 6010 + TCP 6001 |
| Pairing 401/403 | Expired token | Re-issue `POST /api/pairing/issue` |
| Config push not applied | configuration_version not bumped / agent offline | Apply template with bump; check DSA heartbeat |
| RAA register 403 | Enrollment key mismatch | Align `EnrollmentKey` with `RA_AGENT_ENROLLMENT_KEY` |
| Download ticket expired | >10 min | Re-issue ticket from Deployment Center |

## Security model (Phase 1)

- Portal remains authority for bookings and RA; DSA for Equipment PC local config
- Pairing tokens short TTL
- Secrets: Windows Credential Manager on PCs; Fernet patterns on Portal for RDP secrets
- Installers: SHA-256 + signature status displayed; prefer signed builds in production
