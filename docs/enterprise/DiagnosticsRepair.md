# Diagnostics and Repair

## Diagnostics

`POST /api/v1/lab/infrastructure/nodes/{node_id}/diagnostics/` returns PASS/WARN/FAIL checks (heartbeat, version, CPU, disk, tunnel, optional commissioning snapshot).

## Repair actions (no reinstall)

`POST .../repair/` with `action` in:

- repair, reconfigure, recommission
- restart_agent, refresh_configuration
- rescan_software, retry_synchronization

DSA refresh/reconfigure sets `bootstrap_required` (and restart flag when applicable). All actions write `LabRepairAction` + `LabAuditEvent`.
