# Rollback Guide — Remote Analysis

**Use when:** Pilot deploy fails health checks, sessions unstable, or migration/regression discovered.

---

## Principles

1. Prefer **forward fix** for small config errors (`mock_guacamole`, Guacamole URL).  
2. Roll back **Portal image** and **agent MSI/binaries** together only if API contracts diverge.  
3. Never `migrate` backwards on production without a tested reverse plan and backup.  

---

## Immediate mitigation (no full rollback)

| Symptom | Action |
|---------|--------|
| Guacamole down | Set maintenance message; optionally re-enable mock **only** for non-RDP demos (not for real users) |
| Bad agent build | Stop service on PCs; redeploy previous agent folder; start service |
| Allocation storm | Disable workstations; pause beat queue task if needed |
| Workspace disk full | Raise alert; disable uploads; free archive |

---

## Portal rollback steps

1. **Announce** freeze on new remote sessions.  
2. **Terminate** active RA sessions from ops UI/API where possible.  
3. **Restore** previous Portal container image tag.  
4. **Do not** reverse migrations unless the release added `0008` and you must remove the index only — dropping an index is low risk; reversing earlier RA migrations is **high risk**.  
5. Restart celeryworker + celerybeat.  
6. Verify `/api/v1/analysis/health/ready/`.  
7. Spot-check reservation list + one mock or live session.  

### If `0008` must be undone

```sql
-- PostgreSQL example; prefer Django reverse only if migration is reversible
DROP INDEX IF EXISTS ra_ws_status_hb_idx;
```

Then mark migration unapplied only with DBA oversight.

---

## Agent rollback

1. `Stop-Service RemoteAnalysisAgent`  
2. Restore previous binaries under `C:\Services\RemoteAnalysisAgent`  
3. Keep `ProgramData\...\State` (token) unless rotating credentials  
4. `Start-Service RemoteAnalysisAgent`  
5. Confirm heartbeat  

---

## Guacamole rollback

Redeploy previous Guacamole compose stack from backup; keep Portal `RA_GUACAMOLE_*` aligned.

---

## Data restore

1. Stop writers (django/celery).  
2. Restore PostgreSQL backup taken pre-change.  
3. Restore workspace volume if file exchange corrupted.  
4. Start services; run Administrator checklist.  

Details: `DisasterRecovery.md`.

---

## Validation after rollback

- [ ] Health ready 200  
- [ ] Agents ONLINE  
- [ ] No unexpected FAILED sessions spike  
- [ ] Post-incident notes filed  
