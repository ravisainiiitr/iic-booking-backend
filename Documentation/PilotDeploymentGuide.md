# Pilot Deployment Guide — Five Analysis Workstations

**Audience:** IIT Roorkee platform admins / lab IT  
**Scale:** Initial pilot — **5** analysis PCs + shared Portal/Guacamole  

Related: `DeploymentGuide.md`, `RemoteAnalysisAgent.md`, `BrowserRemoteDesktop.md`, `DisasterRecovery.md`.

---

## 1. Server requirements (Portal)

| Component | Pilot minimum |
|-----------|---------------|
| Portal (Django/Gunicorn) | 4 vCPU, 8 GB RAM |
| PostgreSQL | 2 vCPU, 4 GB RAM, backup enabled |
| Redis | 1 GB RAM |
| Celery worker + beat | Co-located or separate; 2 vCPU |
| Guacamole + guacd + DB | 2 vCPU, 4 GB RAM |
| Storage | Workspace volume sized for 5 users × quota |

OS: Linux containers per `docker-compose.production.yml` (+ optional `docker-compose.guacamole.yml`).

---

## 2. Certificates & TLS

- Public HTTPS on Portal and Guacamole via Traefik/campus certs  
- Agent `PortalBaseUrl` must be `https://…`  
- Set Guacamole `verify_tls` appropriately for internal CA  

---

## 3. Firewall rules

| Source | Destination | Port | Purpose |
|--------|-------------|------|---------|
| Analysis PCs | Portal | 443 | Agent API |
| Users (browser) | Portal | 443 | UI / API |
| Users (browser) | Guacamole public URL | 443 | RDP tunnel |
| Portal (internal) | Guacamole API | 8080/443 | REST orchestration |
| Guacamole | Analysis PCs | 3389 | RDP |
| Admins | Portal / Flower (if used) | restricted | Ops |

**Do not** expose agent `LocalHealthPort` (5088) beyond loopback.

---

## 4. Guacamole deployment

1. Deploy guacd + Guacamole + DB.  
2. Set env: `RA_MOCK_GUACAMOLE=false`, `RA_GUACAMOLE_*` URLs and admin credentials.  
3. `python manage.py sync_remote_analysis_settings`  
4. Confirm `GET /api/v1/analysis/health/ready/` → `checks.guacamole=ok`.  

---

## 5. Agent installation (each of 5 PCs)

1. Install .NET 10 Windows Desktop/Runtime.  
2. Publish/copy `RemoteAnalysisAgent` to `C:\Services\RemoteAnalysisAgent`.  
3. Configure `PortalBaseUrl`, **`EnrollmentKey`** (must match portal `RA_AGENT_ENROLLMENT_KEY`), display name, department/room.  
4. Run `scripts\install-service.ps1` as Administrator (automatic startup).  
5. Verify `http://127.0.0.1:5088/api/health` and portal workstation ONLINE within ~30s.  
6. Store RDP secrets in Portal for that workstation (encrypted).

Windows: enable RDP, ensure guacd can reach 3389, disable sleep on AC power, allow agent service recovery on failure.

---

## 6. Backup

- Nightly PostgreSQL dump  
- Workspace/archive volume snapshots  
- Guacamole DB backup  
- Document restore in `DisasterRecovery.md` / `RollbackGuide.md`  

---

## 7. Monitoring

- Portal readiness + Compose healthcheck  
- Operations dashboard (available/busy/offline, queue, alerts)  
- Agent logs under `C:\ProgramData\RemoteAnalysisAgent\Logs\`  
- Guacamole server logs  

---

## 8. Upgrade process

1. Announce maintenance window.  
2. Backup DB + volumes.  
3. Drain sessions / avoid new reservations.  
4. Deploy Portal image; `migrate --noinput`.  
5. Restart celeryworker/beat.  
6. Rolling update agents if binary changed.  
7. Run Administrator checklist.  

---

## 9. Rollback

See `Documentation/RollbackGuide.md`.

---

## 10. Validation checklist (pilot day)

- [ ] Five agents ONLINE with recent heartbeats  
- [ ] Guacamole readiness `ok`  
- [ ] Create reservation → session → browser RDP for one faculty user  
- [ ] Concurrent session on second PC  
- [ ] Terminate → cleanup command COMPLETED  
- [ ] Workspace upload/download  
- [ ] Ops dashboard KPIs non-empty  
- [ ] Email or portal notification received (as configured)  
- [ ] Failure drill: stop one agent → OFFLINE → restart → ONLINE  

---

## 11. Rollout strategy

Week 1: 2 PCs + admins only → Week 2: 5 PCs + selected faculty → Week 3: students under lab in-charge supervision.
