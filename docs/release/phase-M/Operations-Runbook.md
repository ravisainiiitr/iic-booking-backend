# Operations Runbook — Institute Production (v2.5.0)

**Portal:** https://equip.iitr.ac.in  
**Host:** EC2 `ubuntu@…` · checkout `/home/ubuntu/iic-booking-backend`  
**Frontend:** `/home/ubuntu/iic-booking-frontend`  
**Audience:** On-call / system administrators  
**Companions:** [Production-Deployment-Guide.md](../deploy/Production-Deployment-Guide.md), [Administrator-Checklists.md](Administrator-Checklists.md)

---

## 1. Daily health checks

| # | Check | Command / UI |
|---|--------|----------------|
| 1 | Portal home | `curl -fsS -o /dev/null -w '%{http_code}\n' https://equip.iitr.ac.in/` → `200` |
| 2 | API ready | `curl -fsS https://equip.iitr.ac.in/api/v1/analysis/health/ready/` |
| 3 | Django container | `docker ps --filter name=django --format '{{.Status}}'` → `healthy` |
| 4 | Frontend container | `docker ps --filter name=frontend --format '{{.Status}}'` → `healthy` |
| 5 | Redis / Celery | `docker exec iic_booking_production_redis redis-cli PING`; `docker exec iic-booking-backend-celeryworker-1 celery -A config.celery_app inspect ping` |
| 6 | Disk | `df -h /` — keep **&lt;70%** |
| 7 | DSA | Windows: `DepartmentSyncAgent` Running; portal agent `online=true`; local `http://127.0.0.1:6001/api/health` |
| 8 | Remote Analysis | Windows: `RemoteAnalysisAgent` Running; health `http://127.0.0.1:5088/health` → `AVAILABLE` (when idle) |
| 9 | Guacamole stack | `docker ps --filter name=guacamole` all Up/healthy |

---

## 2. Backup verification

**Nightly job (production):**

```cron
30 2 * * * TZ=Asia/Kolkata /home/ubuntu/bin/iic-nightly-backup-cron.sh
```

- Output: `/home/ubuntu/backups/nightly/nightly-YYYYMMDD/db/portal.sql.gz`  
- Symlink: `/home/ubuntu/backups/nightly/latest`  
- Log: `/var/log/iic-nightly-backup.log`  
- Retention: 14 days  

**Daily verify (integrity):**

```bash
/home/ubuntu/bin/iic-restore-verify.sh /home/ubuntu/backups/nightly/latest
```

**Weekly verify (live temp restore on RDS):**

```bash
VERIFY_RESTORE_DB=1 PG_MAJOR=17 /home/ubuntu/bin/iic-restore-verify.sh /home/ubuntu/backups/nightly/latest
```

Repo equivalents: `scripts/ops/iic-nightly-backup.sh`, `scripts/ops/iic-restore-verify.sh`, `scripts/deploy/backup.sh`.

---

## 3. Restore procedure

> Prefer point-in-time / snapshot restore via **AWS RDS** console for production disasters. Logical dump restore is for verification and last-resort rebuilds.

1. Put portal in maintenance / stop writers if restoring production DB.  
2. Take a fresh pre-restore dump: `/home/ubuntu/bin/iic-nightly-backup-cron.sh`  
3. Restore logical dump into a **new** database first and smoke-test, or restore RDS from snapshot.  
4. Point `DATABASE_URL` only after validation.  
5. Restart:  
   `cd /home/ubuntu/iic-booking-backend && docker compose -f docker-compose.production.yml up -d`  
6. Smoke: login → list equipment → DSA heartbeat → RA health.  
7. Confirm agents reconnected.

**Integrity-only check (no production overwrite):**

```bash
VERIFY_RESTORE_DB=1 PG_MAJOR=17 /home/ubuntu/bin/iic-restore-verify.sh /home/ubuntu/backups/nightly/latest
```

---

## 4. Docker maintenance

```bash
# Status
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
docker stats --no-stream

# Safe reclaim (unused images + build cache)
docker builder prune -af
docker image prune -af

# Logs (bounded)
docker logs --tail=200 iic-booking-backend-django-1
docker logs --tail=200 iic-booking-backend-celeryworker-1

# Recreate one service after config change
cd /home/ubuntu/iic-booking-backend
docker compose -f docker-compose.production.yml up -d --force-recreate django
```

Never prune **named volumes** without a restore plan.

---

## 5. Log rotation

- System: `/etc/logrotate.d/*`  
- Journal: keep capped (`journalctl --vacuum-size=200M` if growth returns)  
- Backup log: `/var/log/iic-nightly-backup.log` — rotate monthly or via logrotate snippet if large  
- Docker JSON logs: monitor `docker system df`; configure log driver size limits if needed  

---

## 6. DSA monitoring

| Signal | Expectation |
|--------|-------------|
| Windows service | `DepartmentSyncAgent` Automatic / Running |
| Local health | `http://127.0.0.1:6001/api/health` → Healthy |
| Portal | Agent online; heartbeat age &lt; 60s |
| Upload queue | Local `/api/uploads`; portal results after Active-folder drop |
| Watch path | Profile UNC/path reachable (IIC: `D:\Results`) |

**Incident:** service stop → Event Viewer → portal URL/enroll → re-bootstrap profile → verify booking cache sync.

---

## 7. Remote Analysis monitoring

| Signal | Expectation |
|--------|-------------|
| Service | `RemoteAnalysisAgent` Automatic / Running |
| Health | `:5088/health` → `AVAILABLE` when idle |
| Portal workstation | Enabled; fresh heartbeat; not stuck BUSY/RESERVED without reservation |
| Guacamole | Gateway + guacd + guacamole healthy |
| Commands | `/api/v1/analysis/commands/history/` recent COMPLETED |

**Stuck RESERVED/BUSY:** enqueue admin `CLEAN_WORKSTATION`. Prefer `/analysis/end/` over `/analysis/release/` for cleanup.

---

## 8. Incident response

| Symptom | First actions |
|---------|----------------|
| Portal 5xx / down | `docker ps`; django logs; Redis/RDS; redeploy last good tag |
| Frontend blank | Frontend container health; `https://equip.iitr.ac.in/` and `/config.js` |
| DSA offline | Windows service + network + enroll secret |
| Results missing | Active folder name = booking reference; UploadQueue; S3 |
| RA cannot allocate | Workstation AVAILABLE; no orphan hold; CLEAN if needed |
| Disk &gt;70% | `docker builder prune -af`; journal vacuum; investigate `/var/lib/docker` |
| Backup failed | Tail `/var/log/iic-nightly-backup.log`; pull `postgres:17`; check RDS SG |

Escalate with: time (UTC), release tag, container statuses, last good backup path, user impact.

---

## 9. Upgrade procedure

1. Announce maintenance window if needed.  
2. Confirm nightly backup succeeded (or run manual backup).  
3. Tag release on GitHub.  
4. Deploy:  
   `gh workflow run "Deploy Backend" -f release_tag=vX.Y.Z`  
5. Watch workflow; on host `git describe --tags` matches.  
6. Smoke: health ready, login, book, DSA heartbeat, RA health.  
7. Frontend (if changed): deploy via frontend runner / compose recreate.  

---

## 10. Rollback procedure

1. Identify last known-good tag (e.g. `v2.5.0-rc24-release` / `v2.5.0`).  
2. Redeploy that tag via **Deploy Backend**.  
3. If schema-incompatible: restore DB from pre-upgrade dump / RDS snapshot **before** starting new code, or restore then redeploy matching tag.  
4. Recreate frontend from previous compose/image if UI broken.  
5. Verify agents and smoke tests.  

Repo helper: `scripts/deploy/rollback.sh` (environment-specific).

---

## Contacts / ownership

| Area | Owner |
|------|--------|
| Portal / deploy | Backend release engineer |
| RDS / AWS | Cloud / infra admin |
| DSA / lab PCs | Department IT + Lab Incharge |
| Remote Analysis / Guacamole | RA platform owner |
| End-user support | IIC office / helpdesk |

Update names/emails in [Support Contacts](Support-Contacts.md) for the Final package.
