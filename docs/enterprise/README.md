# Enterprise Laboratory Infrastructure (Phase 2)

Portal Main Admin dashboard for fleet visibility, configuration lifecycle, health, repair, updates, and audit — without changing Phase-1 onboarding.

## Architecture

```
Portal Lab Infrastructure UI
        │
        ▼
Portal aggregate APIs (/api/v1/lab/)
   ├── DSA (heartbeats + equipment_pcs rollup)
   └── RAA (fleet inventory + heartbeats)
```

Control planes unchanged: Portal→DSA→Equipment PC and Portal→RAA→Analysis PC.

## Key APIs

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/lab/infrastructure/` | Fleet tree |
| GET | `/api/v1/lab/infrastructure/nodes/{id}/` | Node detail |
| POST | `/api/v1/lab/infrastructure/nodes/{id}/repair/` | Self-heal action |
| POST | `/api/v1/lab/infrastructure/nodes/{id}/diagnostics/` | Diagnostics report |
| GET | `/api/v1/lab/alerts/` | Unified alerts |
| GET | `/api/v1/lab/audit/` | Unified audit |
| GET/POST | `/api/v1/lab/configuration/profiles/{id}/` | Config history / push |
| POST | `/api/v1/lab/configuration/profiles/{id}/rollback/` | Rollback |
| POST | `/api/v1/lab/configuration/ack/` | DSA config ack (agent auth) |
| GET | `/api/v1/lab/software/compliance/` | Required vs installed |
| GET | `/api/v1/lab/testing/` | Phase 2.5 / Lab SAT Execution dashboard (Main Admin) |
| GET | `/api/v1/lab/testing/wizard/` | Current guided SAT case |
| POST | `/api/v1/lab/testing/evidence/` | Attach screenshot/log/config/capture |
| GET/POST | `/api/v1/lab/testing/defects/` | Defect management linked to SAT |
| GET | `/api/v1/lab/testing/health/` | Live health panel |
| GET | `/api/v1/lab/testing/readiness/` | Production readiness score + checklist |
| GET | `/api/v1/lab/testing/runs/{id}/report/` | SAT report (`format=json\|csv\|xlsx\|pdf`) |
| GET/POST | `/api/v1/lab/testing/runs/` | List / start SAT runs |
| GET | `/api/v1/lab/testing/results/` | Drill-down results |
| PATCH | `/api/v1/lab/testing/results/{id}/` | Record pass/fail |

## UI

- `/laboratory-infrastructure` — Main Admin fleet dashboard (20s auto-refresh)
- `/test-dashboard` — Lab SAT Execution Mode (wizard, evidence, defects, reports)
- Phase-1 `/deployment-center`, wizard, installers remain unchanged

## Stabilization & Lab SAT

Plans and operator guide: [`docs/phase-2.5/`](../phase-2.5/README.md) and [Lab-SAT-Execution-Mode.md](../phase-2.5/Lab-SAT-Execution-Mode.md). No new business modules — SAT tooling and defect fixes only.

## Heartbeat protocol

- **RAA:** CPU/Memory/Disk + diskFreeBytes, windowsUptimeSeconds, reverseTunnelStatus, agentVersion
- **DSA:** existing fields + `equipment_pcs[]` rollup from local Equipment PC status posts to `:6001`
- Equipment PCs never call Portal directly

## Configuration push

Template/profile change → bump `configuration_version` → `bootstrap_required` → DSA bootstrap → Equipment PC apply → `POST /api/v1/lab/configuration/ack/`

Bootstrap documents include `configuration_signature` (HMAC-SHA256).

## Health detectors

```bash
python manage.py run_lab_health_detectors
```

Detects: DSA/Analysis offline, config drift, disk full, Equipment PC errors, duplicate registrations.

## Docs index

- [FleetArchitecture.md](./FleetArchitecture.md)
- [HeartbeatProtocol.md](./HeartbeatProtocol.md)
- [ConfigurationPush.md](./ConfigurationPush.md)
- [DiagnosticsRepair.md](./DiagnosticsRepair.md)
- [MonitoringAlerts.md](./MonitoringAlerts.md)
- [AgentUpdates.md](./AgentUpdates.md)
- [DeploymentCenter.md](./DeploymentCenter.md)
- [HealthDashboard.md](./HealthDashboard.md)
- [Reporting.md](./Reporting.md)
- [Troubleshooting.md](./Troubleshooting.md)
