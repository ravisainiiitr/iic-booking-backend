# Pilot Deployment Checklist — First Analysis Workstation

**Audience:** Platform admin + lab IT  
**Goal:** One production-like PC end-to-end (Portal → Agent → Guacamole → RDP → sync)

Use with: `DeploymentValidationReport.md`, `scripts/HealthCheck.*`, `VerifyPortal.ps1`, `VerifyAgent.ps1`, `TroubleshootingGuide.md`, `RollbackGuide.md`.

---

## A. Portal

- [ ] Production compose / Traefik HTTPS live
- [ ] `.envs/.production/.django` contains secrets (never commit):
  - [ ] `DJANGO_SECRET_KEY`
  - [ ] `DJANGO_DEBUG=False` / production settings module
  - [ ] `REDIS_URL`
  - [ ] `RA_MOCK_GUACAMOLE=false`
  - [ ] `RA_GUACAMOLE_BASE_URL`, `RA_GUACAMOLE_API_URL`
  - [ ] `RA_GUACAMOLE_ADMIN_USERNAME`, `RA_GUACAMOLE_ADMIN_PASSWORD`, `RA_GUACAMOLE_DATA_SOURCE`
  - [ ] `RA_AGENT_ENROLLMENT_KEY` (strong random)
  - [ ] Optional: `RA_APPLY_ENV_SETTINGS=true` **or** run `sync_remote_analysis_settings`
- [ ] `python manage.py migrate` (includes `remote_analysis.0010_workspace_lifecycle_phases`)
- [ ] Celery **worker** + **beat** running
- [ ] Workspace / archive volume mounted and writable
- [ ] `./scripts/HealthCheck.sh https://<portal>` → readiness PASS
- [ ] Manage user can open `/api/v1/analysis/operations/diagnostics/?view=html`

---

## B. Guacamole

- [ ] guacd + Guacamole + Guacamole DB deployed
- [ ] Admin password rotated from any default / `change-me`
- [ ] Portal can reach Guacamole API (internal URL)
- [ ] Readiness shows `checks.guacamole=ok`
- [ ] Guacamole can reach analysis PC **TCP 3389**

---

## C. Agent (first Windows PC)

- [ ] Windows updated; RDP enabled; firewall allows Guacamole → 3389
- [ ] .NET runtime installed (as required by agent publish)
- [ ] Publish/install via `RemoteAnalysisAgent/scripts/install-service.ps1`
- [ ] `appsettings.json`: `PortalBaseUrl=https://…`, `EnrollmentKey=<same as RA_AGENT_ENROLLMENT_KEY>`
- [ ] Service **RemoteAnalysisAgent** Automatic + Running
- [ ] `VerifyAgent.ps1 -PortalBaseUrl https://…` → PASS
- [ ] Portal shows workstation registered; heartbeat age &lt; 90s
- [ ] Software inventory appears under Portal software / workstation detail

---

## D. Configuration verification

- [ ] Diagnostics: `DEBUG=false`, `mock_guacamole=false`
- [ ] `VerifyPortal.ps1 -BaseUrl … -Token …` → PASS
- [ ] Enrollment key required on register (no open register)

---

## E. Functional verification (pilot)

- [ ] Booking completed with DSA/results available
- [ ] Start Analysis → workstation allocated
- [ ] Workspace `sync_phase` reaches **InputReady**
- [ ] Browser Guacamole session launches
- [ ] Input files present under session `Input/`
- [ ] Save a file under `Output/`
- [ ] End session → collect → **UploadVerified** / **Completed**
- [ ] Output visible on Portal workspace / booking analysis files
- [ ] Agent Output deleted only after verify (or deferred if fail)

---

## F. Rollback (if pilot fails)

- [ ] Disable workstation in Portal (maintenance/disable)
- [ ] Stop agent service on PC
- [ ] Set `RA_MOCK_GUACAMOLE=true` **only** for lab recovery (never leave on prod)
- [ ] Prefer forward fix; see `RollbackGuide.md` for Guacamole/Portal image rollback
- [ ] Preserve ProgramData session Output if collect failed before wipe

---

## Sign-off

| Role | Name | Date | Result |
|------|------|------|--------|
| Portal admin | | | |
| Lab IT | | | |
| Pilot user | | | |
