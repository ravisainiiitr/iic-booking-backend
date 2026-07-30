# Administrator Acceptance Checklist — Remote Analysis

Execute before enabling production/pilot traffic. Sign and date each section.

**Environment:** _________________ **Date:** ________ **Admin:** ________

---

## A. Platform & security

- [ ] `DEBUG=False` in production settings  
- [ ] `RA_AGENT_ENROLLMENT_KEY` set; agents use matching `EnrollmentKey`  
- [ ] TLS valid on Portal (certificate chain trusted)  
- [ ] TLS valid on Guacamole public URL  
- [ ] `mock_guacamole=False`  
- [ ] `GET /api/v1/analysis/health/ready/` returns **200** with `database=ok`, `guacamole=ok`, `agent_enrollment=configured`  
- [ ] Secrets not in git; Guacamole admin password rotated from defaults  
- [ ] Redis/DB network-restricted  

## B. Data & jobs

- [ ] Migrations applied through `remote_analysis.0008_*`  
- [ ] Celery worker running  
- [ ] Celery beat running; RA periodic tasks present  
- [ ] Backup job verified in last 24h  

## C. Agents (repeat per workstation)

| # | Hostname | Agent registered | Heartbeat &lt; 2 min | RDP secret set | Health ≥ threshold | Notes |
|---|----------|------------------|----------------------|----------------|--------------------|-------|
| 1 | | ☐ | ☐ | ☐ | ☐ | |
| 2 | | ☐ | ☐ | ☐ | ☐ | |
| 3 | | ☐ | ☐ | ☐ | ☐ | |
| 4 | | ☐ | ☐ | ☐ | ☐ | |
| 5 | | ☐ | ☐ | ☐ | ☐ | |

- [ ] Loopback health OK on each PC (`127.0.0.1:5088/api/health`)  

## D. Functional smoke

- [ ] Session allocation works (reservation → RESERVED/ACTIVE path)  
- [ ] Browser remote desktop connects (live Guacamole)  
- [ ] Cleanup successful after terminate (`CLEAN_*` COMPLETED)  
- [ ] Workspace upload + download  
- [ ] Scheduler operational (queue processes; expire works)  
- [ ] Audit logs generated for session start/stop  
- [ ] Notifications delivered (portal and/or email)  
- [ ] Ops dashboard shows available / busy / offline counts  

## E. Failure drills (sample)

- [ ] Stop one agent → OFFLINE within timeout  
- [ ] Restart agent → ONLINE  
- [ ] Portal web restart → agents reconnect  

## Sign-off

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Platform admin | | | |
| Lab in-charge | | | |

**Decision:** ☐ Ready for pilot  ☐ Hold (list blockers below)

Blockers: _______________________________________________
