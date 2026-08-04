# Operational Readiness Checklist

## Roles and Readiness

| Area | Checklist | Status |
|---|---|---|
| Administrator | Can access deployment center, release controls, and architecture docs | Pending formal run-through |
| Department Admin | Can manage department equipment mappings and provisioning workflows | Pending UAT sign-off |
| Lab Incharge | Can monitor fleet, alerts, maintenance, diagnostics, SAT dashboards | Pending UAT sign-off |
| Operator | Can run session workflows and interpret failure states/readiness panels | Pending UAT sign-off |

## Platform Components

| Component | Checklist | Status |
|---|---|---|
| Equipment PC | Provisioning/onboarding and status reporting validated | Pending staged validation |
| DSA | Enrollment, heartbeat, sync, config ack, upload pipeline validated | Build-qualified; integration pending |
| RAA | Registration, heartbeat, tunnel/session lifecycle validated | Build-qualified; integration pending |
| Maintenance | Maintenance window execution and node maintenance actions validated | Pending operational drill |
| Deployment Center | Installer metadata, ticketed download, compatibility surfacing validated | Pending artifact dry-run |

## Observability and Reliability

| Area | Checklist | Status |
|---|---|---|
| Monitoring | Health dashboards and diagnostics endpoints accessible | Partially validated |
| Backups | Database and artifact backup/restore drills documented and rehearsed | Pending rehearsal evidence |
| Logs | Central log collection/query paths verified for portal + agents | Pending integrated verification |
| Alerts | Alert generation/ack/escalation flows verified | Pending integrated verification |

## Minimum Operational Exit Criteria

- All role-based workflows executed at least once in staging.
- DSA and RAA heartbeat stability sustained over agreed soak window.
- Deployment center installer publish/download/rollback dry-run completed.
- Backup restore test completed with documented evidence.
- Critical alerts route to on-call workflow with acknowledgement audit.
