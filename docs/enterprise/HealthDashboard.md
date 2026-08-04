# Health Dashboard

Main Admin page: `/laboratory-infrastructure`

- Fleet tree with status badges (online/offline/busy/maintenance/…)
- Node detail: versions, CPU/RAM/disk, heartbeat, IP/MAC
- Actions: diagnostics + self-heal repair commands
- Tabs: Fleet, Alerts, Audit, Software, Updates
- Auto-refresh every 20 seconds

Aggregate API: `GET /api/v1/lab/infrastructure/`
