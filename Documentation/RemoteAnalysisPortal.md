# Remote Analysis Portal Module (Milestone 2)

The Equipment Booking Portal is the **single source of truth** for Remote Analysis workstations.

This module does **not** implement Apache Guacamole, browser sessions, or machine scheduling.

## Architecture

```
Equipment Booking Portal
│
├── remote_analysis (Django app)
│      ├── Workstations
│      ├── Inventory
│      ├── Commands
│      ├── Monitoring / Health
│      ├── Audit
│      └── Telemetry
│
└── Remote Analysis Agents
       ├── PC-01 … PC-N
```

Agents are nearly stateless aside from a local cache (AgentId + portal token). All authoritative state lives in the Portal.

## Django app

`iic_booking.remote_analysis`

Layers: models · serializers · services · selectors · permissions · views · admin · signals · urls · migrations

## Database models

| Model | Purpose |
|-------|---------|
| AnalysisWorkstation | Enterprise workstation registry |
| WorkstationHeartbeat | Heartbeat history |
| WorkstationInventory | Hardware inventory snapshot |
| InstalledSoftware | Software inventory + presence |
| SoftwareInventoryHistory | Add / remove / version-change history |
| SoftwareLicense | License metadata (hashed keys only) |
| RemoteCommand | Command queue |
| CommandExecution | Execution history |
| AgentToken | Hashed tokens with expiry / rotation / revocation |
| WorkstationEvent | Audit log |
| TelemetrySnapshot | Metric history |
| WorkstationCapability | Capability profile |
| WorkstationStateHistory | Full status transition history |

## Registration flow

1. Agent `POST /api/v1/analysis/register/` (unauthenticated first contact).
2. Portal validates `agentId`, creates or updates workstation (no duplicates).
3. Portal issues `AgentToken` (plaintext returned once).
4. Agent stores token locally; subsequent calls use `Authorization: Bearer` + `X-Agent-Id`.

## Heartbeat flow

1. Agent `POST /api/v1/analysis/heartbeat/` every ~30s.
2. Portal stores `WorkstationHeartbeat`, updates telemetry, recalculates health.
3. Detects high CPU, low memory, disk full, missed heartbeats → offline.

## Inventory flow

1. Agent `POST /api/v1/analysis/inventory/`.
2. Portal syncs software/licenses/capabilities; records ADDED / REMOVED / VERSION_CHANGED.
3. Audit event written.

## Command queue

States: `PENDING` → `DELIVERED` → (`RUNNING`) → `COMPLETED` | `FAILED` | `EXPIRED`

Supported: `PING`, `REFRESH`, `REFRESH_SOFTWARE`, `COLLECT_LOGS`, `RESTART_AGENT`, `PREPARE_WORKSTATION`, `CLEAN_WORKSTATION`

## Health engine

Score **0–100** from heartbeat age, CPU/memory/disk, inventory freshness, agent version, recent command failures.

## REST APIs

### Agent

- `POST /api/v1/analysis/register/`
- `POST /api/v1/analysis/heartbeat/`
- `POST /api/v1/analysis/inventory/`
- `GET /api/v1/analysis/commands/`
- `POST /api/v1/analysis/commands/{id}/complete/`

### Portal admin

- `GET /api/v1/analysis/workstations/`
- `GET /api/v1/analysis/workstations/{id}/`
- `GET /api/v1/analysis/dashboard/`
- `POST /api/v1/analysis/workstations/{id}/maintenance|enable|disable/`
- `POST /api/v1/analysis/workstations/{id}/commands/`
- plus software / heartbeats / events / command history

## RBAC

Manage: System Administrator (`admin`), Department Administrator (`dept_admin`), Officer In Charge (`manager`), or `remote_analysis.manage`.

View: managers above, Lab Incharge (`operator`), or `remote_analysis.view`.

Students have no access by default.

## UI

Frontend route: `/remote-analysis`

Tabs: Dashboard · Workstations · Installed Software · Heartbeat History · Commands · Maintenance · Health · Audit

## Security

- Agent tokens hashed at rest; rotation + revocation supported.
- No Guacamole configuration, RDP passwords, or end-user credentials stored.
- Portal remains the only orchestrator for future browser sessions.

## Future scheduler (not implemented)

Authenticate → allocate workstation → create Guacamole connection → notify agent → prepare → browser session → end → cleanup.
