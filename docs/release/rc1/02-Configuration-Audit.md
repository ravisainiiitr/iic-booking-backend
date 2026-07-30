# Remote Analysis RC1 — Configuration Audit

## Classification legend

- **Required** — Must be set for a production-ready readiness probe / safe operation  
- **Optional** — Sensible defaults exist; tune for site policy  
- **Development only** — Must not be used in production  
- **Secret** — Store in secret manager / sealed env; never commit  

## Portal / Django (host application)

| Key | Class | Notes |
|-----|-------|-------|
| `DEBUG` | Development only | Must be `False` |
| `SECRET_KEY` | Secret / Required | Fernet for RDP + Guac temp passwords derives from this |
| `ALLOWED_HOSTS` | Required | Portal hostnames |
| `DATABASE_URL` / DB settings | Required | Postgres recommended |
| `REDIS_URL` / `CELERY_BROKER_URL` | Required* | Required for Celery beat/worker; cache may degrade without |
| `FRONTEND_URL` | Optional | Login redirect for HTML consoles |
| `MEDIA_ROOT` | Required | Workspace/archive storage parent |

\*Mark Redis N/A only if Celery is intentionally disabled (not recommended for RA).

## Remote Analysis env

| Key | Class | Notes |
|-----|-------|-------|
| `RA_MOCK_GUACAMOLE` | Development only in prod | Must be `false` |
| `RA_GUACAMOLE_BASE_URL` | Required (desktop) | Public HTTPS Guacamole URL |
| `RA_GUACAMOLE_API_URL` | Required (desktop) / Secret-adjacent | Internal API base |
| `RA_GUACAMOLE_ADMIN_USERNAME` | Secret / Required (desktop) | |
| `RA_GUACAMOLE_ADMIN_PASSWORD` | Secret / Required (desktop) | |
| `RA_GUACAMOLE_DATA_SOURCE` | Optional | Default `postgresql` |
| `RA_GUACAMOLE_VERIFY_TLS` | Optional | Default true |
| `RA_AGENT_ENROLLMENT_KEY` | Secret / Required | Required when `DEBUG=False` |
| `RA_APPLY_ENV_SETTINGS` | Optional | `true` to persist overlays on boot |

## RemoteAnalysisSettings (DB singleton)

| Key | Class | Production value |
|-----|-------|------------------|
| `mock_guacamole` | Development only | `False` |
| `guacamole_base_url` / `api_url` | Required (desktop) | Live URLs |
| `session_timeout` / `idle_timeout` | Optional | Defaults 120 / 15 min |
| `max_concurrent_sessions` | Optional | Default 50 |
| `single_active_session_per_booking` | Optional | Default True |
| `clipboard_*` / `file_transfer_*` / `audio_enabled` | Optional | Site policy |
| `workspace_root` / `archive_root` | Optional | Empty → MEDIA defaults |
| `virus_scanner` | Optional | `noop` only today |
| `recording_enabled` | Optional | Keep False |

## Per-workstation

| Item | Class |
|------|-------|
| `WorkstationRdpSecret` (username/password/domain/port) | Secret / Required for live RDP |
| Agent token issued at enrollment | Secret / Required |
| Workstation inventory hostname/IP reachable from guacd | Required |

## Agent (Analysis PC)

| Key | Class |
|-----|-------|
| `PortalBaseUrl` | Required |
| Enrollment / bearer token | Secret / Required |
| `SessionWorkspaceRoot` | Optional |
| Heartbeat / poll intervals | Optional |

## Sample production `.env` (Portal)

```bash
# --- Django core ---
DEBUG=False
SECRET_KEY=change-me-use-long-random
ALLOWED_HOSTS=portal.example.com
FRONTEND_URL=https://portal.example.com

# --- Data plane ---
DATABASE_URL=postgres://ra_user:CHANGE_ME@db:5432/iic_booking
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/0

# --- Remote Analysis ---
RA_MOCK_GUACAMOLE=false
RA_GUACAMOLE_BASE_URL=https://guac.example.com/guacamole
RA_GUACAMOLE_API_URL=http://guacamole:8080/guacamole
RA_GUACAMOLE_ADMIN_USERNAME=guacadmin
RA_GUACAMOLE_ADMIN_PASSWORD=CHANGE_ME_STRONG
RA_GUACAMOLE_DATA_SOURCE=postgresql
RA_GUACAMOLE_VERIFY_TLS=true
RA_AGENT_ENROLLMENT_KEY=CHANGE_ME_LONG_RANDOM
RA_APPLY_ENV_SETTINGS=true
```

After deploy: `python manage.py migrate` then `python manage.py sync_remote_analysis_settings`.

Full catalog: `iic_booking/remote_analysis/configuration_catalog.py`.
