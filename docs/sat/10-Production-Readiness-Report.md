# 10 — Production Readiness Report

**Fill and sign only after SAT system-level PASS.**  
Until then, treat sections as a template populated with known architecture; **Sign-off must remain unsigned**.

| Field | Value |
|-------|-------|
| Report version | 1.0-draft |
| SAT completion date | _pending_ |
| Portal release SHA | |
| Agent release version | |
| Environment | |

---

## 1. Overall architecture

```
Users / Portal UI
       │
       ▼
Equipment Booking Portal (Django)
  └── remote_analysis
        ├── Workstation registry & health
        ├── Command queue
        ├── Workspaces & file transfer
        ├── Reservations / (optional Guacamole)
        └── Operations / Commissioning console
       │
       ▼ HTTPS
Remote Analysis Agent (Windows service)
  ├── Heartbeat / inventory
  ├── Command handlers (PREPARE / COLLECT / CLEAN / …)
  └── Session filesystem under ProgramData
```

Portal is source of truth. Agent is nearly stateless aside from local state + session files.

## 2. Components

| Component | Tech | Notes |
|-----------|------|-------|
| Portal API | Django + DRF | Token + Session auth |
| DB | PostgreSQL (prod) | SQLite only for local/tests |
| Agent | .NET Worker | Windows service |
| Optional | Redis, Celery, Guacamole | Per deployment |
| Ops UI | Commissioning + diagnostics HTML | Manage RBAC |

## 3. Deployment topology

| Tier | Recommendation |
|------|----------------|
| Portal | HA behind TLS terminator; sticky sessions if using session auth for HTML ops |
| DB | Managed Postgres; backups enabled |
| Agents | One service per Analysis PC; outbound HTTPS only to portal |
| Secrets | Enrollment key, DB, Guacamole admin — vault / env, not git |

Document actual hostnames/VNets in the signed copy.

## 4. Security review

| Control | Status |
|---------|--------|
| Agent Bearer tokens hashed at rest | |
| Enrollment key in non-DEBUG | |
| Manage APIs: IsAuthenticated + CanManageRemoteAnalysis | |
| Commissioning HTML anonymous → login redirect | |
| CSRF on session POSTs | |
| No query-token JSON auth | |
| SAT-07 evidence attached | |

Residual risks: see [09-Known-Limitations.md](09-Known-Limitations.md).

## 5. Scalability review

| Topic | Finding |
|-------|---------|
| Heartbeat cardinality | Baseline from SAT-08 |
| Concurrent workspaces | Baseline from SAT-08 |
| Large file limits | Proxy/body size; measured 1 GB |
| Bottlenecks | DB, disk I/O on agent, portal media storage |

## 6. Operational checklist

- [ ] Migrations applied (`remote_analysis` head)
- [ ] Enrollment key set
- [ ] FRONTEND_URL correct for login redirect
- [ ] Agent services monitored (service state + heartbeat age)
- [ ] Log shipping for portal + agent
- [ ] Commissioning console reachable by admins only
- [ ] On-call runbook = [07-Recovery-Procedures.md](07-Recovery-Procedures.md)

## 7. Backup strategy

| Asset | Method | RPO / RTO target |
|-------|--------|------------------|
| Postgres | Automated snapshots + PITR if available | |
| Portal media / workspace files | Object storage versioning or FS backup | |
| Agent state | Recreatable via re-register; session data not sole backup | N/A |
| Secrets | Vault backup | |

## 8. Disaster recovery

1. Restore DB to last good backup.
2. Restore media if file rows reference blobs.
3. Redeploy portal at known-good SHA.
4. Agents: restart; re-register if tokens invalidated.
5. Smoke: SAT-02 heartbeat + SAT-05 abbreviated path.

## 9. Monitoring checklist

- [ ] Portal `/health/live/` and `/health/ready/`
- [ ] Agent offline alert (heartbeat age)
- [ ] Command FAILED / EXPIRED rate
- [ ] Disk free on Analysis PCs
- [ ] TLS cert expiry
- [ ] 5xx rate on `/api/v1/analysis/*`

## 10. Production sign-off checklist

- [ ] SAT checklist complete (all PASS/N/A)
- [ ] No open S1/S2
- [ ] Perf baselines recorded
- [ ] Security review complete
- [ ] Backups verified with test restore
- [ ] Rollback plan documented
- [ ] Change ticket approved

| Role | Name | Date | Sign |
|------|------|------|------|
| Product owner | | | ☐ |
| Engineering lead | | | ☐ |
| Security | | | ☐ |
| Operations | | | ☐ |
| SAT lead | | | ☐ |

**Decision:** ☐ Ready for production · ☐ Not ready (attach blockers)
