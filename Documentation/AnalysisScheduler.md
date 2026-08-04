# Remote Analysis Scheduler (Milestone 3)

Intelligent workstation reservation and allocation inside the Equipment Booking Portal.

**Does not** implement Apache Guacamole, browser sessions, RDP launch, or session streaming.
The scheduler only decides *which* workstation is assigned and *when*.

## Architecture

```
Equipment Booking Portal
│
├── Reservation Engine
├── SchedulerService
├── AvailabilityEngine
├── AllocationService (candidate scoring)
├── ConflictResolver
├── ReservationQueue
└── Remote Analysis Agents
```

Portal remains the single orchestrator. Agents stay nearly cache-only.

## Reservation lifecycle

```
REQUESTED → VALIDATING → QUEUED|RESERVED → PREPARING → READY → ACTIVE
                ↓
         COMPLETED | EXPIRED | CANCELLED | FAILED
```

Full history persisted in `ReservationHistory`.

## Allocation algorithm

1. Validate reservation (window, booking linkage, no duplicate active reservation).
2. Find candidates via `AvailabilityEngine`.
3. Score with configurable weights (health, CPU, memory, recent usage, software match, capability match, department affinity, idle time).
4. Reserve highest-scoring eligible workstation.
5. On no candidate → enqueue (priority + FIFO).

## Availability (never allocate)

- Offline / disabled / maintenance / calibration / software update / hardware fault / cleaning / error / registering / reserved / busy / preparing
- Health score below threshold (default 50)
- Missed heartbeats / expired agent tokens
- Active maintenance windows (see `Documentation/MaintenanceMode.md`)
- Overlapping active reservations
- Missing **required** equipment software (`required_software_names` hard filter — see `SoftwareMappingGuide.md`)
- Extreme current CPU load

When a PC enters maintenance, queued users are notified and the reservation queue is reprocessed against remaining compatible PCs.

## Conflict resolution

Detects double booking, maintenance overlap, offline workstation, extension conflicts, priority overrides. All decisions audited.

## APIs

| Method | Path |
|--------|------|
| POST/GET | `/api/v1/analysis/reservations/` |
| GET | `/api/v1/analysis/reservations/{id}/` |
| POST | `/api/v1/analysis/reservations/{id}/cancel/` |
| POST | `/api/v1/analysis/reservations/{id}/extend/` |
| GET | `/api/v1/analysis/availability/` |
| GET | `/api/v1/analysis/candidates/` |
| GET | `/api/v1/analysis/scheduler/status/` |
| GET | `/api/v1/analysis/scheduler/dashboard/` |
| GET | `/api/v1/analysis/queue/` |

Reservations may reference `equipment.Booking` — no duplicate booking logic.

## Background jobs (Celery)

- `remote_analysis.expire_reservations`
- `remote_analysis.process_reservation_queue`
- `remote_analysis.refresh_workstation_health`
- `remote_analysis.monitor_maintenance_windows`
- `remote_analysis.detect_reservation_conflicts`
- `remote_analysis.refresh_availability_snapshot`

Seeded as PeriodicTasks on migrate.

## Dashboard (UI)

`/remote-analysis` tabs: Scheduler · Reservations · Queue · Availability  
(+ existing workstation management tabs)

## Session launch (implemented — Milestone 4+)

Flow is live: authenticate → allocate (scheduler) → create Guacamole connection → agent prepare → browser launch → end → cleanup.  
See `Documentation/BrowserRemoteDesktop.md`. Mock Guacamole remains available for CI only (`mock_guacamole`); production must use live Guacamole.

## Permissions

Manual reservations: System Admin, Department Admin, Officer In Charge (`remote_analysis.manage`).  
Students do not allocate workstations directly.
