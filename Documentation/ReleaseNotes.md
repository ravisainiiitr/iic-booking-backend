# Release Notes — Remote Analysis Production Pilot

**Product:** IIC Equipment Booking — Remote Analysis  
**Release:** Phase 3 validation package (post WS1–WS4)  
**Date:** 2026-07-30  

---

## Highlights

- Windows Remote Analysis Agent (register, heartbeat, inventory, commands, prepare/cleanup, workspace sync, local health)  
- Production Guacamole wiring (`RA_*` env, sync command, readiness)  
- Automated tests ≥90% coverage on `iic_booking.remote_analysis` (112 tests)  
- Hardening: correlation IDs, Guacamole retry, pagination, workstation heartbeat index, Compose healthcheck  
- Phase 3 validation documentation package for IITR pilot  

---

## Components / version matrix

| Component | Version / note |
|-----------|----------------|
| Portal app | Booking backend with `remote_analysis` migrations through **0008** |
| Remote Analysis Agent | `1.0.0` / TFM **net10.0-windows** |
| Guacamole | Ops-deployed (compatible with portal REST client) |
| Python | Project venv (Django 5.2.x as locked) |
| Redis / Celery | As production compose |

---

## Database migration order

Apply in order (Django handles dependencies):

1. `remote_analysis.0001_initial_remote_analysis`  
2. `0002_scheduler_reservation_engine`  
3. `0003_browser_remote_desktop_guacamole`  
4. `0004_analysis_workspace_file_exchange`  
5. `0005_operations_center`  
6. `0006_collaboration_center`  
7. `0007_production_hardening_indexes`  
8. `0008_workstation_status_heartbeat_index`  

```bash
python manage.py migrate remote_analysis
```

---

## Required environment variables

| Variable | Purpose |
|----------|---------|
| `RA_MOCK_GUACAMOLE` | Must be `false` in production |
| `RA_AGENT_ENROLLMENT_KEY` | Shared secret; required for readiness when `DEBUG=False`; agents send as `X-Enrollment-Key` |
| `RA_GUACAMOLE_BASE_URL` | Public Guacamole URL |
| `RA_GUACAMOLE_API_URL` | Internal REST API URL |
| `RA_GUACAMOLE_ADMIN_USERNAME` / `PASSWORD` | Admin REST auth |
| `RA_GUACAMOLE_DATA_SOURCE` | Optional data source name |
| `RA_GUACAMOLE_VERIFY_TLS` | Optional TLS verify override |
| `RA_APPLY_ENV_SETTINGS` | Persist overlays when true |
| Standard Django/Redis/DB secrets | Portal production |

Agent: `RemoteAnalysisAgent:PortalBaseUrl`, `EnrollmentKey`, intervals, `LocalHealthPort`.

---

## Known limitations

- Virus scanner default `noop`  
- SMS/WhatsApp/Push notifications not implemented  
- Session recording metadata only  
- Live RDP requires Guacamole ops deployment  
- Frontend E2E / full load test outside this package  

---

## Verification

See `ProductionReleaseChecklist.md` and `AdministratorChecklist.md`.

Automated: `pytest iic_booking/remote_analysis/tests` — **112 passed**, **90%** coverage (2026-07-30).  
Agent: Release build **0 warnings / 0 errors**.
