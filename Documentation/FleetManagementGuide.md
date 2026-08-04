# Fleet Management Guide

Administrator guide for Analysis PC identity, health, and duplicate cleanup.

## Persistent identity

Each Analysis PC is identified by a **machine fingerprint**:

```
mg:{MachineGuid}|bios:{BiosUuid}
```

- Derived from Windows MachineGuid (registry) and BIOS UUID (WMI).
- Survives Agent reinstall, reboot, IP/LAN changes.
- Portal registration reconnects an existing row when the fingerprint matches, instead of creating a duplicate.

`agent_id` remains the Agent’s local token key; on fresh install it prefers MachineGuid.

## Duplicate cleanup

```http
GET  /api/v1/analysis/fleet/duplicates/
POST /api/v1/analysis/fleet/duplicates/
```

Auto-merge hostname/fingerprint groups:

```json
{ "auto": true }
```

Manual merge:

```json
{
  "survivor_id": "<uuid>",
  "duplicate_ids": ["<uuid>", "..."],
  "archive": true
}
```

Archived rows are disabled and renamed so they cannot collide on future reconnects. Historical reservations/sessions are reassigned to the survivor.

## Fleet inventory

```http
GET /api/v1/analysis/fleet/
GET /api/v1/analysis/fleet/inventory/
GET /api/v1/analysis/fleet/inventory/?status=OFFLINE
```

Shows status counts, heartbeats, health, tunnel/RDP status, current booking, software inventory age.

## Configuration audit

```http
GET /api/v1/analysis/equipment/config-audit/
```

Lists every Remote Analysis–enabled equipment and missing RAW / RESULTS / software / session settings.

## Commissioning

```http
GET|POST /api/v1/analysis/commissioning/run/
```

Returns overall `PASS` / `WARNING` / `FAIL` with recommendations.
