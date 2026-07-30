# Deployment Validation Report — Remote Analysis

**Date:** 2026-07-30  
**Scope:** Portal (`iic-booking-backend`) + Agent (`RemoteAnalysisAgent`)  
**Mode:** Verification only (no new product features)

Related: `scripts/HealthCheck.ps1`, `scripts/HealthCheck.sh`, `scripts/VerifyPortal.ps1`, `scripts/VerifyAgent.ps1`, `PilotDeploymentChecklist.md`, `TroubleshootingGuide.md`.

---

## 1. Configuration inspection

### Environment variables (Portal)

| Variable | Purpose | Production expectation | Finding |
|----------|---------|------------------------|---------|
| `DJANGO_DEBUG` / `DEBUG` | Django debug | **False** | Production settings default False; local settings force True |
| `DJANGO_SECRET_KEY` | Secrets | Required, non-default | Must be set in `.envs/.production/.django` |
| `REDIS_URL` / Celery broker | Celery + cache | Redis reachable | Compose wires Redis; local may use memory/eager |
| `RA_MOCK_GUACAMOLE` | Override mock Guacamole | **false** | Empty → uses DB; DB **defaults mock=True** |
| `RA_GUACAMOLE_BASE_URL` | Public Guacamole URL | HTTPS URL | Not in compose by default — ops must set |
| `RA_GUACAMOLE_API_URL` | Internal Guacamole REST | Reachable from Django | Ops must set |
| `RA_GUACAMOLE_ADMIN_USERNAME` / `_PASSWORD` | Guacamole admin | Strong secret | Ops must set; never expose to browsers |
| `RA_GUACAMOLE_DATA_SOURCE` | Guacamole DS name | Match Guacamole | Ops must set |
| `RA_GUACAMOLE_VERIFY_TLS` | TLS verify | true (or false only with internal CA policy) | Optional overlay |
| `RA_AGENT_ENROLLMENT_KEY` | Agent register gate | **Required** when DEBUG=False | Readiness fails if missing |
| `RA_APPLY_ENV_SETTINGS` | Persist RA_* into DB | true on boot in prod OR run sync command | Default false |
| `CELERY_CONCURRENCY` etc. | Worker knobs | Sized for load | Compose worker start script |

**No committed `.env.example` for RA_\*.** Pattern: Cookiecutter `.envs/.production/.django` (not in git).

### Dangerous / invalid defaults

| Item | Default | Risk | Mitigation |
|------|---------|------|------------|
| `RemoteAnalysisSettings.mock_guacamole` | **True** | Fresh DB ships in mock RDP | Set `RA_MOCK_GUACAMOLE=false`; readiness **503** when DEBUG=False + mock |
| `virus_scanner` | `noop` | No malware scan on uploads | Accept for pilot; plan ClamAV later |
| Guacamole compose DB password | `change-me` (commented stack) | Weak secret | Rotate before go-live |
| Agent `appsettings.Development.json` | `http://127.0.0.1:8000` | Dev only | Publish with production HTTPS PortalBaseUrl |
| Open register without enrollment key | Allowed when key unset | Bootstrap risk | Require key in production (readiness enforces when DEBUG=False) |

### Agent configuration

| Key | Default | Production note |
|-----|---------|-----------------|
| `PortalBaseUrl` | required | Must be `https://…` public Portal |
| `EnrollmentKey` | empty | Must match `RA_AGENT_ENROLLMENT_KEY` |
| `SessionWorkspaceRoot` | ProgramData\…\Sessions | Ensure disk quota + NTFS ACL |
| `LocalHealthPort` | 5088 | Loopback only — do not firewall-expose |
| Heartbeat / command intervals | 30s / 10s | OK for pilot |

### Storage

- `workspace_root` / `archive_root` empty → under `MEDIA_ROOT/remote_analysis/…`
- Production: dedicated volume, backup, monitoring of free space vs quotas

### Celery

- Beat: `DatabaseScheduler` + RAA periodic tasks (`retry_failed_workspace_collects`, `interval_workspace_collect`, retention, etc.)
- Production must run **worker + beat** (not `CELERY_TASK_ALWAYS_EAGER`)

### API endpoints (LB / ops)

| Probe | Path |
|-------|------|
| Liveness | `GET /api/v1/analysis/health/live/` |
| Readiness | `GET /api/v1/analysis/health/ready/` |
| Combined | `GET /api/v1/analysis/health/` |
| Diagnostics (manage) | `GET /api/v1/analysis/operations/diagnostics/` (+ `?view=html`) |

---

## 2. Automated verification results (CI / this workspace)

| Check | Result |
|-------|--------|
| Agent Release build | **PASS** (0 warnings / 0 errors) |
| Portal RA pytest | **PASS** — 128 tests |
| Coverage `iic_booking.remote_analysis` | **90%** |
| Migrations present | `0009` + `0010_workspace_lifecycle_phases` in tree |
| Local DB migrate status | **`0010` may be unapplied** on ops DB — run `migrate` before pilot |

---

## 3. Potential production issues

1. **Leaving mock Guacamole on** — readiness fails closed when DEBUG=False, but lab DBs created earlier may still have mock=True until env sync.  
2. **Missing RA_\* in production env file** — Guacamole never configured.  
3. **Celery beat not running** — sync retries / interval collect / retention stall.  
4. **Workspace volume not mounted / not writable** — prepare/ingest fails.  
5. **Agent EnrollmentKey mismatch** — register 403; no heartbeats.  
6. **Firewall: Guacamole → PC:3389** blocked — RDP sessions fail after InputReady.  
7. **TLS / internal CA** — agent or Guacamole client verify failures.  
8. **Migration 0010 not applied** — lifecycle phase choices / `upload_verified_at` missing.

---

## 4. Deployment blockers requiring manual infrastructure action

See final summary in the chat / checklist. Software verification scripts cannot replace:

- Windows PC imaging, RDP enablement, local admin for agent service  
- Guacamole + guacd deployment and network path to analysis PCs  
- Firewall / VPN / certificates  
- Production secrets in `.envs/.production/.django`  
- Applying migrations on production Postgres  

---

## 5. Pass criteria for “ready to pilot”

- [ ] `DEBUG=False`  
- [ ] `GET …/health/ready/` → 200 with `guacamole=ok`, `agent_enrollment=configured`  
- [ ] `RA_MOCK_GUACAMOLE=false` (and DB mock false)  
- [ ] Worker + beat running; RAA beat tasks enabled  
- [ ] Workspace/archive roots writable  
- [ ] First agent registered; heartbeat age &lt; 90s  
- [ ] Diagnostics page shows no critical warnings  
- [ ] `migrate` includes `remote_analysis.0010`  
