# Remote Analysis — Administrator Guide (RC1)

## Responsibilities

- Portal/Django production settings and secrets  
- Database, Redis, Celery worker/beat  
- Guacamole gateway deployment and TLS  
- Enrollment key and workstation RDP secrets  
- Permission groups (`remote_analysis.manage` / `.view`)  
- Backups and DR drills  

## Initial production setup

1. Apply configuration from [02-Configuration-Audit.md](02-Configuration-Audit.md)  
2. Follow [03-Deployment-Checklist.md](03-Deployment-Checklist.md)  
3. Confirm readiness: `GET /api/v1/analysis/health/ready/` → `status=ready`  
4. Enable equipment `enable_remote_analysis` only for commissioned instruments  
5. Issue agent enrollment; verify heartbeats  

## Admin surfaces

| Tool | Path |
|------|------|
| Django admin | `RemoteAnalysisSettings`, workstations, RDP secrets, sessions |
| Diagnostics Toolkit | `/api/v1/analysis/operations/toolkit/?view=html` |
| Commissioning console | `/api/v1/analysis/operations/commissioning/?view=html` |
| Session dashboard | `/api/v1/analysis/session/dashboard/` |
| Commissioning evidence | `/api/v1/analysis/operations/toolkit/runs/<id>/evidence/` |

## Permissions

- **Manage:** toolkit, commissioning, terminate sessions, ops actions  
- **View:** dashboards/read APIs  
- **Booking owner:** launch remote desktop for own booking  

Do not weaken these for RC1.

## Guacamole

See [RemoteAnalysisGuacamoleConfiguration.md](../../RemoteAnalysisGuacamoleConfiguration.md) and Security review.
