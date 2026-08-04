# Analysis PC Maintenance Mode

Enterprise operational-state management for Remote Analysis workstations (IIT Roorkee CIF).

## Operational states

| Status | Allocatable | Heartbeat | Notes |
|--------|-------------|-----------|-------|
| Available / Online | Yes | Updates status | Ready for scheduler |
| Busy / Preparing / Reserved | No (in use) | Protected | Session / reservation held |
| Cleaning | No | Protected | Post-session wipe |
| Maintenance | No | Continues | Scheduled / reactive maintenance |
| Calibration | No | Continues | Instrument / PC calibration |
| Software Update | No | Continues | Patch / install windows |
| Hardware Fault | No | Continues | Faulty — admin attention |
| Offline | No | Marks offline when missed | Connectivity loss |
| Disabled | No | Protected | Admin hard-disable |

Maintenance PCs **continue sending Agent heartbeats** so administrators can monitor health while the scheduler excludes them from allocation.

## Administrator configuration (per window)

Each maintenance window supports:

- **Kind** — Maintenance / Calibration / Software Update / Hardware Fault / Cleaning / Offline / Disabled
- **Reason** — short user-facing label
- **Detailed description**
- **Start** and **Expected end**
- **Assigned engineer / vendor**
- **AMC reference**
- **Ticket number**
- **Maintenance notes**
- **Restore status** — normally `AVAILABLE` when the window ends

Recurring windows (`recurrence_rule`) are reserved for a future release.

## APIs

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/analysis/fleet/` | Fleet dashboard counts + active windows |
| GET/POST | `/api/v1/analysis/maintenance/windows/` | List / schedule windows |
| POST | `/api/v1/analysis/maintenance/windows/<id>/end/` | End early and restore |
| POST | `/api/v1/analysis/workstations/<id>/maintenance/` | Immediate maintenance (accepts kind, end, engineer, ticket, …) |
| GET | `/api/v1/analysis/dashboard/` | Includes `fleet` summary |

## Scheduler behaviour

1. `AvailabilityEngine` treats all non-operational statuses as **blocking**.
2. Active `MaintenanceWindow` rows also block the covered time range.
3. Periodic task `remote_analysis.monitor_maintenance_windows`:
   - Applies windows that have started
   - Restores workstations when windows end
   - Notifies queued users
   - Reprocesses the reservation queue
4. If a PC enters maintenance while users wait, they receive:

> This Analysis Workstation is currently undergoing scheduled maintenance. Your request has automatically been reassigned to the next suitable workstation.

5. If **all** compatible PCs are under maintenance, the experience API shows:

> No compatible Analysis Workstation is currently available.  
> Reason: Scheduled Maintenance  
> Estimated Availability: Today 4:30 PM

## Fleet dashboard fields

- Total Analysis PCs
- Available
- Busy
- Maintenance
- Calibration
- Offline
- Faulty (hardware fault + error)

## Django admin

`MaintenanceWindow` admin lists kind, engineer, ticket, AMC, and active flag.
