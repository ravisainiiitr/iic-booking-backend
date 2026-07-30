# Failure Simulation — Remote Analysis

**Date:** 2026-07-30  
**Purpose:** Expected graceful recovery behavior for pilot ops. Validate each row on staging.

| Scenario | Expected behavior | Verification |
|----------|-------------------|--------------|
| Analysis PC loses network | Heartbeats stop; after offline threshold workstation → OFFLINE; reservations stop allocating to it; queue holds users | Disconnect NIC; watch status + ops dashboard |
| Agent crashes / service stopped | Same as offline; Windows service recovery (if configured) restarts agent; re-registers or resumes with stored token | `Stop-Service`; confirm OFFLINE then ONLINE |
| Portal restarts | Stateless web workers; Celery continues; agents retry HTTP; sessions DB-backed | Rolling restart django; heartbeats resume |
| Database restart | Readiness → 503 until DB up; writes fail briefly; Celery autoretries OperationalError | Restart Postgres; `/health/ready/` |
| Guacamole restart | New sessions fail until healthy; readiness `guacamole=unreachable`; in-flight may drop; terminate/cleanup still attempted | Restart Guacamole; readiness; new session |
| Power failure (PC) | Offline detection; on boot agent auto-starts (service); cleanup may be needed if session was ACTIVE | Cold boot; PREPARE/CLEAN commands |
| Heartbeat timeout | `mark_stale_workstations_offline` / health refresh | Stop agent > offline seconds |
| Cleanup failure | Command marked failed; audit/alert; workstation may stay BUSY/DIRTY until retry CLEAN | Force fail cleanup; re-queue CLEAN |
| License / software unavailable | Prep may succeed but user workflow fails; ops assist via collaboration | Document lab software inventory |
| Session timeout / idle | Celery expire/idle cleanup; Guacamole teardown; CLEAN queued | Lower idle timeout in test settings |
| User disconnect | Session may go IDLE then expire; reconnect policy depends on Guacamole + token validity | Disconnect RDP client |
| Browser refresh | Launch token single-use — refresh after connect should use Guacamole client session, not re-consume token | UAT reconnect scenario |
| Redis down | Celery degraded; cache readiness may show degraded; web may still serve DB paths | Stop Redis; observe worker/ready |

---

## Design properties supporting recovery

- Agent HTTP retries + re-register on auth loss  
- Celery `acks_late` + limited autoretry on RA periodic tasks  
- Guacamole client one-shot retry + 401 re-auth  
- Commands persisted in DB until completed  
- Audit events for auth/session/workspace actions  

---

## Pilot requirement

Execute the table on the five-PC pilot stack and attach timestamps/screenshots to the administrator sign-off package.
