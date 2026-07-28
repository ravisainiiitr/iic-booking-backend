# Operations Center

Milestone 6 of the Remote Analysis Platform.

## Architecture

```
Equipment Booking Portal
  ├── Operations Center (dashboards)
  ├── Analytics Engine
  ├── Utilization Engine
  ├── Alert Engine
  ├── Reporting Engine
  ├── Capacity Planner / Availability Engine
  ├── Performance Monitor
  └── Existing RA platform telemetry (consumed, not redesigned)
```

Portal remains the operational command center. Agent, Scheduler, Guacamole sessions, and Analysis Workspace architectures are unchanged.

## Analytics Engine

Aggregates `RemoteDesktopSession` / `SessionStatistics` / workspace telemetry into:

- Total sessions, duration stats, idle %, prepare/launch/cleanup/sync latencies  
- Reconnects, cancellation rate, no-show rate, success rate  
- Hourly `OperationalKPI` snapshots  

## KPI definitions

| KPI | Source |
|-----|--------|
| Online / Busy / Available workstations | `AnalysisWorkstation.status` |
| Average utilization | `WorkstationUtilization` |
| Session / reservation success | session & reservation status ratios |
| Workspace transfer success | `WorkspaceTransfer` COMPLETED ratio |
| Avg prepare / launch / sync / cleanup | session + workspace telemetry |
| Availability % | heartbeat-derived uptime |
| Queue length | waiting `ReservationQueue` |
| Open alerts | `AlertEvent` OPEN/ACKNOWLEDGED |

## Alert flow

1. Default `AlertRule` rows seeded on evaluate  
2. Celery `evaluate_alerts` every 5 minutes  
3. Conditions: agent offline, heartbeat timeout, high CPU/memory/disk, session/sync failures, conflicts, quota, idle sessions  
4. Deduplicated open events → acknowledge / resolve APIs  
5. Audited under `AuditCategory.ALERTS`  

## Capacity planning

- Peak concurrent sessions (hourly buckets)  
- Department / day-of-week / hour-of-day demand  
- Occupancy vs unused capacity  
- Rule-based predicted need (`avg_daily * 1.1`) — **not ML**  

Availability: operational %, MTBF, MTTR, heartbeat reliability.

## Report generation

`POST /api/v1/analysis/reports/generate/` with `report_type` + `format` (`JSON`/`CSV`/`EXCEL`/`PDF`).  
Uses `reportlab` + `openpyxl`. Files stored under `MEDIA/remote_analysis/reports/` (relative paths only).

## Dashboard design

`GET /api/v1/analysis/operations/dashboard/` returns executive, operations (live WS/sessions/reservations), performance, utilization, capacity, availability, alerts, trends. Cached in `DashboardSnapshot` (~60s).

UI: Remote Analysis **Operations** tab.

## Celery tasks

| Task | Cadence |
|------|---------|
| `aggregate_hourly_kpis` | hourly |
| `aggregate_daily_utilization` | daily |
| `evaluate_alerts` | 5m |
| `refresh_operations_dashboard` | 5m |
| `generate_weekly_reports` / `generate_monthly_reports` | daily |
| `archive_old_metrics` | daily |

## Future predictive analytics

Out of scope for Milestone 6: machine learning, predictive maintenance, external APM, software licensing, cloud monitoring.

Migration: `0005_operations_center`.
