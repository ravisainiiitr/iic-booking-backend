# Master Commissioning Checklist

Use status: `PASS` / `FAIL` / `BLOCKED` / `NOT APPLICABLE`.

## Core Platform

| Item | Check | Status | Owner | Evidence |
|---|---|---|---|---|
| Portal | API health, auth, RBAC, admin workflows |  |  |  |
| Frontend | Build deployed, route integrity, role visibility |  |  |  |
| Deployment Center | Release metadata, ticketed download, compatibility matrix |  |  |  |
| Department Sync Agent (DSA) | Enrollment, heartbeat, command polling, config ACK |  |  |  |
| Equipment Wizard | Discovery, pairing, provisioning, config validation |  |  |  |
| Remote Analysis Agent (RAA) | Registration, heartbeat, inventory, command completion |  |  |  |

## Remote Analysis Stack

| Item | Check | Status | Owner | Evidence |
|---|---|---|---|---|
| Remote Analysis | Session create/launch/connect/terminate/end/upload |  |  |  |
| Guacamole | Connection brokering and access controls |  |  |  |
| Reverse Tunnel | Join/close tunnel reliability and failover behavior |  |  |  |
| Session Reservation | Availability and reservation lifecycle |  |  |  |
| Check-in | Reservation check-in policy and window behavior |  |  |  |
| OTP | OTP-based access/verification where applicable |  |  |  |

## Infrastructure Dependencies

| Item | Check | Status | Owner | Evidence |
|---|---|---|---|---|
| S3/Object Storage | Upload, archive, download, lifecycle permissions |  |  |  |
| SMTP | Email notifications and delivery audit |  |  |  |
| Redis | Queue/scheduler/cache health |  |  |  |
| Postgres | Migration head, backups, transactional integrity |  |  |  |

## Lab and Device Plane

| Item | Check | Status | Owner | Evidence |
|---|---|---|---|---|
| Equipment PCs | Provisioned, healthy, linked to equipment |  |  |  |
| Analysis PCs | RAA online, reachable, policy-compliant |  |  |  |
| Laboratory Infrastructure | Fleet view, node detail, maintenance controls |  |  |  |
| Fleet | Inventory consistency, duplicate handling |  |  |  |
| Health | Heartbeat and diagnostic surfaces |  |  |  |
| Alerts | Alert generation, acknowledgement, closure workflow |  |  |  |
| Logging | Portal + DSA + RAA logs query/export integrity |  |  |  |

## Business/Operational Features

| Item | Check | Status | Owner | Evidence |
|---|---|---|---|---|
| SAT Dashboard | Run creation, evidence, defect flow, readiness |  |  |  |
| Instrument Utilization | Utilization metrics and reports |  |  |  |
| Consumables | Consumables tracking/report workflow |  |  |  |
| Configuration Push | Template/policy push, ACK, drift detection |  |  |  |
| Software Mapping | Required software mapping and allocation |  |  |  |
| Maintenance Mode | Entry/exit and policy effect on scheduling |  |  |  |
| Queue Handling | Waitlist, queue prioritization, timeout behavior |  |  |  |

## Release and Recovery

| Item | Check | Status | Owner | Evidence |
|---|---|---|---|---|
| Installer Updates | DSA/RAA/Wizard update discover/download/apply |  |  |  |
| Repair Packages | Repair package distribution and execution |  |  |  |
| Rollback | DB restore + app rollback + installer rollback drill |  |  |  |

## Sign-off

| Role | Name | Date | Decision |
|---|---|---|---|
| Release Manager |  |  |  |
| Platform Engineering Lead |  |  |  |
| Lab Operations Lead |  |  |  |
| Security Reviewer |  |  |  |
