# Phase 2.9 - Backend API Inventory (B1-B8)

Methods/authentication are taken from route contracts and DRF view decorator patterns in the owning modules.

## Remote Analysis (Owner: B1/B2)

| Route | Method | Purpose | Authentication | Owning commit |
|---|---|---|---|---|
| `/api/v1/analysis/health/` | GET | Service health | None | B2 |
| `/api/v1/analysis/health/live/` | GET | Liveness probe | None | B2 |
| `/api/v1/analysis/health/ready/` | GET | Readiness probe | None | B2 |
| `/api/v1/analysis/register/` | POST | Agent registration | Agent auth contract | B2 |
| `/api/v1/analysis/heartbeat/` | POST | Agent heartbeat ingest | Agent auth contract | B2 |
| `/api/v1/analysis/inventory/` | POST | Agent/software inventory ingest | Agent auth contract | B2 |
| `/api/v1/analysis/commands/` | GET | Command polling | Agent auth contract | B2 |
| `/api/v1/analysis/commands/<command_id>/complete/` | POST | Command completion | Agent auth contract | B2 |
| `/api/v1/analysis/workstations/` | GET | Workstation list | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/workstations/<workstation_id>/` | GET | Workstation detail | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/workstations/<workstation_id>/maintenance/` | POST | Maintenance state update | Portal manage RBAC | B2 |
| `/api/v1/analysis/workstations/<workstation_id>/enable/` | POST | Enable workstation | Portal manage RBAC | B2 |
| `/api/v1/analysis/workstations/<workstation_id>/disable/` | POST | Disable workstation | Portal manage RBAC | B2 |
| `/api/v1/analysis/workstations/<workstation_id>/commands/` | POST | Issue workstation command | Portal manage RBAC | B2 |
| `/api/v1/analysis/dashboard/` | GET | Operations dashboard summary | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/fleet/` | GET | Fleet dashboard | Portal view RBAC | B2 |
| `/api/v1/analysis/fleet/inventory/` | GET | Fleet inventory details | Portal view RBAC | B2 |
| `/api/v1/analysis/fleet/duplicates/` | GET/POST | Duplicate workstation review/merge | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/commissioning/run/` | GET/POST | Commissioning execution | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/equipment/config-audit/` | GET | Equipment config audit | Portal view RBAC | B2 |
| `/api/v1/analysis/maintenance/windows/` | GET/POST | Maintenance window list/create | Portal manage RBAC | B2 |
| `/api/v1/analysis/maintenance/windows/<window_id>/end/` | POST | End maintenance window | Portal manage RBAC | B2 |
| `/api/v1/analysis/software/` | GET | Installed software inventory | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/commands/history/` | GET | Command history | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/events/` | GET | Workstation/session events | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/heartbeats/` | GET | Heartbeat history | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/reservations/` | GET/POST | Reservation list/create | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/reservations/<reservation_id>/` | GET | Reservation detail | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/reservations/<reservation_id>/cancel/` | POST | Reservation cancel | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/reservations/<reservation_id>/extend/` | POST | Reservation extend | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/availability/` | GET | Availability query | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/candidates/` | GET | Candidate workstation list | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/scheduler/status/` | GET | Scheduler status | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/scheduler/dashboard/` | GET | Scheduler dashboard | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/queue/` | GET | Queue status | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/session/create/` | POST | Create remote session | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/session/dashboard/` | GET | Session dashboard | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/session/history/` | GET | Session history | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/sessions/` | GET | Session list | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/session/<session_id>/launch/` | POST | Launch desktop | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/session/<session_id>/connect/` | GET/POST | Token connect handshake | Token/enrollment/portal auth flow | B2 |
| `/api/v1/analysis/session/<session_id>/terminate/` | POST | Terminate session | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/session/<session_id>/status/` | GET | Session status | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/session/<session_id>/activity/` | POST | Session activity heartbeat | Portal/agent contract | B2 |
| `/api/v1/analysis/session/<session_id>/audits/` | GET | Session audit timeline | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/` | GET/POST | Workspace list/create | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/dashboard/` | GET | Workspace dashboard | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/` | GET | Workspace detail | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/upload/` | POST | Upload workspace file | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/download/` | GET | Download workspace artifact | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/archive/` | POST | Archive workspace | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/restore/` | POST | Restore workspace | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/files/` | GET | Workspace files list | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/sync/` | POST | Trigger sync | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/retry-transfer/` | POST | Retry transfer | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/cancel-transfer/` | POST | Cancel transfer | Portal auth + RA permissions | B2 |
| `/api/v1/analysis/workspaces/<workspace_id>/manifest/` | GET | Agent manifest | Agent/enrollment contract | B2 |
| `/api/v1/analysis/operations/*` | GET/POST | Operations, diagnostics, commissioning toolkit | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/analytics/`, `/utilization/`, `/performance/`, `/capacity/` | GET | Ops analytics views | Portal manage/view RBAC | B2/B6 |
| `/api/v1/analysis/alerts/*` | GET/POST | Alerts list/acknowledge | Portal manage/view RBAC | B2/B5 |
| `/api/v1/analysis/reports/*` | GET/POST | Report list/generation/download | Portal manage/view RBAC | B2/B6 |
| `/api/v1/analysis/collaboration/*` and activity/notifications/comments/share/etc. | GET/POST | Collaboration center flows | Portal auth + collaboration permissions | B2 |
| `/api/v1/analysis/workflows/*` | GET/POST | Workflow designer and mapping | Portal manage/view RBAC | B2 |
| `/api/v1/analysis/installer/*` | GET/POST | RAA installer release distribution | Portal manage/enrollment/agent auth mix | B2/B3 |
| `/api/v1/analysis/updates/discover/` | GET | Agent update discovery | Agent bearer/enrollment/manage auth | B2 |
| `/api/v1/analysis/updates/report/` | POST | Agent update status report | Agent bearer/enrollment/manage auth | B2 |
| `/api/v1/bookings/<booking_id>/analysis/end/` (+ legacy alias) | POST | End analysis | Authenticated user/owner/admin | B2 |
| `/api/v1/bookings/<booking_id>/analysis/start/` (+ legacy alias) | POST | Start checked-in session | Authenticated user/owner/admin | B2 |
| `/api/v1/bookings/<booking_id>/analysis/release/` (+ legacy alias) | POST | Release check-in reservation | Authenticated user/owner/admin | B2 |
| `/api/v1/bookings/<booking_id>/analysis/extend/` (+ legacy alias) | POST | Extend analysis | Authenticated user/owner/admin | B2 |
| `/api/v1/bookings/<booking_id>/analysis/files/upload/` (+ legacy alias) | POST | Upload past data | Authenticated user/owner/admin | B2 |

## Deployment Center (Owner: B3)

| Route | Method | Purpose | Authentication | Owning commit |
|---|---|---|---|---|
| `/api/v1/deployment/center/` | GET | Deployment center summary | Portal manage RBAC | B3 |
| `/api/v1/deployment/wizard/releases/` | GET/POST | Wizard releases list/publish | Portal manage RBAC | B3 |
| `/api/v1/deployment/wizard/releases/latest/` | GET | Latest wizard release | Portal/manage or tokened flow | B3 |
| `/api/v1/deployment/wizard/releases/latest/download-ticket/` | POST | Create latest download ticket | Portal manage/enrollment flow | B3 |
| `/api/v1/deployment/wizard/releases/<release_id>/download-ticket/` | POST | Create release-specific ticket | Portal manage/enrollment flow | B3 |
| `/api/v1/deployment/wizard/download/<token>/` | GET | Ticketed download | Signed ticket auth | B3 |

## Plug-and-Play Platform (Owner: B4)

| Route | Method | Purpose | Authentication | Owning commit |
|---|---|---|---|---|
| `/api/v1/sync/enroll/` | POST | Agent enrollment | Agent enrollment key | B4 |
| `/api/v1/sync/heartbeat/` | POST | Sync heartbeat | Agent auth | B4 |
| `/api/v1/sync/bootstrap/` | GET | Bootstrap config | Agent auth | B4 |
| `/api/v1/sync/equipment/` | GET | Equipment assignment list | Agent auth | B4 |
| `/api/v1/sync/bookings/` | GET | Booking feed for agent | Agent auth | B4 |
| `/api/v1/sync/workspaces/` | POST | Workspace create/update | Agent auth | B4 |
| `/api/v1/sync/commands/*` | GET/POST | Command poll/ack/complete/fail | Agent auth | B4 |
| `/api/v1/sync/uploads/*` | POST | Chunked upload transport | Agent auth | B4 |
| `/api/v1/sync/results/import/` | POST | Result import metadata | Agent auth | B4 |
| `/api/v1/sync/results/finalize/` | POST | Result finalization | Agent auth | B4 |
| `/api/v1/sync/admin/*` | GET/POST | Main Admin sync console APIs | Portal admin auth | B4 |
| `/api/v1/sync/security/*` | POST/GET | Device identity/certificate/api-key operations | Agent/admin auth | B4 |
| `/api/v1/sync/recovery/*` | POST/GET | Reconcile/integrity/conflict flows | Agent/admin auth | B4 |
| `/api/v1/sync/enterprise/*` | GET/POST | Enterprise topology/assignment/drain/retire APIs | Admin auth | B4 |
| `/api/v1/sync/monitoring/*` | GET/POST | Sync monitoring and alert actions | Admin auth | B4 |
| `/api/v1/sync/releases/*` | GET/POST | DSA release publish/deploy/rollback | Admin auth | B4 |
| `/api/v1/sync/updates/*` | GET/POST | Update discover/status/report | Agent/admin auth | B4 |
| `/api/v1/sync/experiments/*` | GET/POST | Experiment and instrumentation endpoints | Agent/admin auth | B4 |
| `/api/v1/sync/operations/*` | GET | Diagnostics/maintenance operational probes | Admin auth | B4 |
| `/api/v1/sync/installer/*` | GET/POST | DSA installer distribution | Admin/enrollment/ticket auth | B4 |
| `/api/v1/sync/configuration/ack/` | POST | Config acknowledgement alias | Agent auth | B4/B5 |

## Laboratory Infrastructure (Owner: B5)

| Route | Method | Purpose | Authentication | Owning commit |
|---|---|---|---|---|
| `/api/v1/lab/infrastructure/` | GET | Fleet/infrastructure dashboard | Admin/ops auth | B5 |
| `/api/v1/lab/infrastructure/nodes/<node_id>/` | GET | Node detail | Admin/ops auth | B5 |
| `/api/v1/lab/infrastructure/nodes/<node_id>/repair/` | POST | Repair action | Admin/ops auth | B5 |
| `/api/v1/lab/infrastructure/nodes/<node_id>/diagnostics/` | POST | Run diagnostics | Admin/ops auth | B5 |
| `/api/v1/lab/infrastructure/nodes/<node_id>/maintenance/` | POST | Maintenance action | Admin/ops auth | B5 |
| `/api/v1/lab/infrastructure/nodes/<node_id>/rotate-credentials/` | POST | Rotate agent secret hint | Admin/ops auth | B5 |
| `/api/v1/lab/alerts/` | GET | Lab alerts list | Admin/ops auth | B5 |
| `/api/v1/lab/alerts/<alert_id>/ack/` | POST | Acknowledge alert | Admin/ops auth | B5 |
| `/api/v1/lab/audit/` | GET | Lab audit log | Admin/ops auth | B5 |
| `/api/v1/lab/configuration/profiles/<profile_id>/` | GET | Configuration history | Admin/ops auth | B5 |
| `/api/v1/lab/configuration/profiles/<profile_id>/rollback/` | POST | Roll back profile | Admin/ops auth | B5 |
| `/api/v1/lab/configuration/ack/` | POST | Configuration acknowledge | Agent/admin auth | B5 |
| `/api/v1/lab/software/compliance/` | GET | Software compliance view | Admin/ops auth | B5 |
| `/api/v1/lab/reports/utilization/` | GET | Utilization reporting | Admin/ops auth | B5/B6 |
| `/api/v1/lab/testing/*` | GET/POST | SAT dashboard/test-run/results/evidence/defects/readiness | Admin/ops auth | B5/B7 |

## Diagnostics (Owner: B6)

| Route | Method | Purpose | Authentication | Owning commit |
|---|---|---|---|---|
| `/api/v1/analysis/operations/diagnostics/` | GET | Deployment diagnostics summary | Portal manage/view RBAC | B2/B6 |
| `/api/v1/sync/operations/diagnostics/` | GET | DSA operations diagnostics | Admin auth | B4/B6 |
| `/api/v1/lab/reports/utilization/` | GET | Lab utilization report | Admin auth | B5/B6 |

## SAT (Owner: B7)

| Route | Method | Purpose | Authentication | Owning commit |
|---|---|---|---|---|
| `/api/v1/lab/testing/` | GET | SAT dashboard home | Admin auth | B5/B7 |
| `/api/v1/lab/testing/runs/` | GET/POST | SAT runs list/create | Admin auth | B5/B7 |
| `/api/v1/lab/testing/runs/<run_id>/` | GET | SAT run detail | Admin auth | B5/B7 |
| `/api/v1/lab/testing/runs/<run_id>/report/` | GET | SAT run report | Admin auth | B5/B7 |
| `/api/v1/lab/testing/results/` | GET/POST | SAT result operations | Admin auth | B5/B7 |
| `/api/v1/lab/testing/results/<result_id>/` | PATCH/POST | SAT result update | Admin auth | B5/B7 |
| `/api/v1/lab/testing/seed/` | POST | Seed SAT catalog | Admin auth | B5/B7 |
| `/api/v1/lab/testing/wizard/` | GET | Current SAT wizard state | Admin auth | B5/B7 |
| `/api/v1/lab/testing/evidence/` | POST | Upload SAT evidence | Admin auth | B5/B7 |
| `/api/v1/lab/testing/defects/` | GET/POST | SAT defect tracking | Admin auth | B5/B7 |
| `/api/v1/lab/testing/health/` | GET | SAT health panel | Admin auth | B5/B7 |
| `/api/v1/lab/testing/readiness/` | GET | SAT readiness recommendation | Admin auth | B5/B7 |

