# Phase L6 — Operational Qualification Report

**Date:** 2026-08-06  
**Host:** `ip-10-0-1-153` / EC2 ap-south-1  
**Deployed tag:** `v2.5.0-rc24-release`

| Check | Result | Notes |
|-------|--------|-------|
| Container set running | PASS | Django healthy; Celery worker/beat; Redis; Guacamole stack; reverse-tunnel gateway |
| Automatic restart policy | PASS | Frontend `unless-stopped`; stack recovers after deploy (~6 min uptime post-rc24) |
| Celery recovery | PASS | Worker ping OK after deploy restart |
| Redis persistence | PASS | `aof_enabled:1`, `rdb_last_bgsave_status:ok` |
| Database | PASS | External `DATABASE_URL` (managed); Guacamole DB container healthy |
| Log rotation | PASS | `/etc/logrotate.d` present (system packages) |
| Disk usage | WARN | **80%** on `/` — action recommended before heavy media growth |
| Deploy backups | PARTIAL | `/home/ubuntu/deploy-backups` has pre-rc1 dump + compose/env snapshots (2026-07-31); no daily cron visible in ubuntu crontab |
| DSA recovery | PASS | L1 offline URL sim + re-enroll/heartbeat; service Automatic |
| RA recovery | PASS | CLEAN_WORKSTATION clears orphan `RESERVED`; rc23 sticky BUSY fix |
| Frontend container health | WARN | Docker healthcheck fails (`/health` missing) but public https://equip.iitr.ac.in/ returns 200 |
| Server reboot drill | DEFERRED | Not executed (disruptive); restart policies + 158-day uptime documented |

## Recommendations

1. Add scheduled PostgreSQL logical backups off-instance; test restore quarterly.  
2. Free disk (≥20% free target) — prune Docker images/logs.  
3. Fix frontend nginx `/health` so Docker health matches reality.

## Verdict

**L6 PASS with WARN** — operationally ready with backup automation and disk cleanup as post-go-live priorities.
