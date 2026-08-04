# Heartbeat Protocol

## DSA → Portal

`POST /api/v1/sync/heartbeat/` with existing fields plus optional:

```json
"equipment_pcs": [
  {
    "id": "...",
    "hostname": "...",
    "mac_address": "...",
    "observed_ip": "...",
    "cpu_percent": 12,
    "memory_percent": 40,
    "disk_used_percent": 55,
    "windows_version": "...",
    "agent_version": "...",
    "configuration_version": 3,
    "share_ok": true,
    "folders_ok": true,
    "sync_status": "idle",
    "last_status_at": "..."
  }
]
```

Portal stores the array in `AgentHeartbeat.details.equipment_pcs`.

## Equipment PC → DSA

`POST /api/equipment-pcs/{id}/status` or `/status-by-mac` on DSA `:6001` (pairing token or loopback).

## RAA → Portal

Existing heartbeat plus Wave 2A extras in payload / `raw_payload`: diskFreeBytes, windowsUptimeSeconds, reverseTunnelStatus, agentVersion, portalReachable, configurationVersion.
