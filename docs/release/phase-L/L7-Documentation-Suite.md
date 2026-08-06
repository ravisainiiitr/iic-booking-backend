# Phase L7 — Documentation Suite (Production-aligned)

**Date:** 2026-08-06  
**Production:** https://equip.iitr.ac.in · Backend `v2.5.0-rc24-release`  
**Canonical existing docs:** `Documentation/` and `docs/deploy/`

This suite summarizes operator-facing procedures verified in Phase L. Prefer existing deep guides for architecture detail.

---

## Administrator Guide

- Portal admin: super_admin / department admin RBAC.  
- Deploy: GitHub Actions **Deploy Backend** with `release_tag=v2.5.0-rcN-release`.  
- Monitor: Django/Celery/Redis/Guacamole containers; Flower optional.  
- Agents: keep DSA + RAA Windows services Automatic; verify heartbeats daily.  
- Wallet / charge profiles / equipment operational status controlled in admin UI.  
- See also: `Documentation/OperationsRunbook.md`, `docs/deploy/Operations-Runbook-IITR.md`.

## Lab Operator Guide

1. Confirm equipment **Operational**.  
2. Manage bookings on Booking Management / Lab Operator dashboard.  
3. Sample lifecycle: receive → (external: Hold / Forward) → **Accept** → run analysis → ensure results land under DSA Active folder → **Complete**.  
4. External users cannot download until I-STEM FBR verified.  
5. Invoice PDF: `/api/bookings/{id}/invoice.pdf`.

## Department Sync Agent Guide

- Service: `DepartmentSyncAgent` · loopback `http://127.0.0.1:6001` · data `C:\ProgramData\DepartmentSyncAgent\`.  
- Enroll via local `POST /api/settings/portal/enroll` with portal enrollment secret.  
- Sync profile watch path for IIC PXRD: `D:\Results` (Active subfolder per booking reference).  
- Heartbeat: portal must show online &lt; 60s.  
- Large uploads resume supported (verified to 1 GB).  
- Offline: restore portal URL; queued bookings reconcile on reconnect.

## Remote Analysis Guide

- Service: `RemoteAnalysisAgent` · health `:5088`.  
- User path: completed booking → Analyze → Start → Guacamole desktop launcher → End.  
- If workstation stuck BUSY/RESERVED with no reservation: enqueue `CLEAN_WORKSTATION` (admin).  
- Sticky BUSY fixed in `v2.5.0-rc23-release`.  
- See: `Documentation/RemoteAnalysisPortal.md`, `ReservationCheckinGuide.md`.

## Installation Guide

- Backend: Docker Compose production stack on EC2 (`/home/ubuntu/iic-booking-backend`).  
- Frontend: nginx container published on host `:8000` behind TLS terminator.  
- Windows: install DSA + RAA as services; open outbound HTTPS to portal; configure enrollment.  
- See: `Documentation/DeploymentGuide.md`, `docs/deploy/Production-Deployment-Guide.md`.

## Backup & Restore Guide

- Current: `/home/ubuntu/deploy-backups` holds historical dump/env snapshots.  
- Redis: AOF enabled.  
- **Action:** schedule nightly `pg_dump` (or managed-DB snapshot) off-box; document restore owner.  
- App rollback: redeploy previous release tag via Deploy Backend workflow.  
- See: `Documentation/RollbackGuide.md`, `Documentation/DisasterRecovery.md`.

## Disaster Recovery Guide

1. Restore DB from latest backup.  
2. Redeploy last known-good tag (`v2.5.0-rc24-release` or newer Final).  
3. Verify Redis/Celery; restart Windows DSA/RAA if needed.  
4. Re-check agent heartbeats and Guacamole gateway health.  
5. Smoke: login → book → sample accept → DSA upload → RA analyze (if enabled).

## Deployment Guide

```text
gh workflow run "Deploy Backend" -f release_tag=v2.5.0-rc24-release
gh run watch <id>
```

Verify: Django healthy, `git describe` on host matches tag, agents online.

## Quick Start Guide

1. Open https://equip.iitr.ac.in and sign in.  
2. Book PXRD (or target equipment) with valid formula inputs.  
3. Send sample; lab accepts (external: Hold → Forward → Accept).  
4. Lab drops results under `D:\Results\Active\<booking_reference>\`.  
5. User/operator downloads (external: after FBR).  
6. Lab completes booking; invoice available.

## Troubleshooting Guide

| Symptom | Action |
|---------|--------|
| DSA offline | Check Windows service; portal URL; enroll; Event Viewer |
| Results not syncing | Confirm Active path equals booking reference; UploadQueue |
| RA stuck BUSY/RESERVED | Admin `CLEAN_WORKSTATION`; confirm no open reservation |
| External cannot accept sample | Ensure Hold/Forward first; backend ≥ rc24 |
| External cannot download | Complete I-STEM FBR verification |
| Frontend Docker unhealthy | Public site may still work; `/health` path missing |

## Known Issues

1. Frontend container healthcheck fails (`wget .../health`) while site serves 200.  
2. Root disk ~80% used.  
3. Automated daily DB backup cron not observed on app host.  
4. Single live DSA / single Analysis PC — concurrent multi-agent not qualified.  
5. `/analysis/release/` may leave reservation `QUEUED`; prefer `/analysis/end/` for cleanup.  
6. DSA uploads UI history can show `Queued` after transport completed.

## L7 Verdict

**PASS** — documentation set generated and cross-checked against production behavior in Phase L.
