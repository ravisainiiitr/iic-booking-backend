# Browser-Based Remote Desktop (Apache Guacamole)

Milestone 4 of the Remote Analysis Platform.

## Architecture

```
Equipment Booking Portal (orchestrator)
        │
   Session Orchestrator
        │
 GuacamoleIntegrationService
        │
 Apache Guacamole (RDP gateway only)
        │
 Windows RDP → Remote Analysis Workstation
        │
 Remote Analysis Agent (prepare / clean / health)
```

- **Portal** owns reservation validation, authorization, session lifecycle, tokens, cleanup, and audit.
- **Guacamole** only transports remote desktop pixels/input.
- **RAA** only prepares and cleans workstations and reports health.
- Scheduling and reservation logic stay in the Portal (Milestone 3).

## Session lifecycle

| State | Meaning |
|-------|---------|
| CREATED | Session row created |
| PREPARING | `PREPARE_WORKSTATION` issued to agent |
| READY | Agent acknowledged prepare |
| TOKEN_GENERATED | Ephemeral Guacamole connection + token ready |
| LAUNCHED | Browser launch URL issued |
| CONNECTING / CONNECTED / ACTIVE | Browser attached |
| IDLE | No activity near idle timeout |
| DISCONNECTING → COMPLETED / TERMINATED / EXPIRED / FAILED | Teardown |

Every transition is persisted in `SessionStateHistory`.

### Launch flow

1. Validate reservation active + user authorized  
2. Confirm workstation healthy and agent online  
3. Issue `PREPARE_WORKSTATION`  
4. Wait for agent acknowledgement (Celery + command complete hook)  
5. Create ephemeral Guacamole user + RDP connection  
6. Issue one-time session token  
7. `GET /api/v1/analysis/session/{id}/launch/` returns Portal launch URL  
8. Browser hits `/connect/` which consumes the token  
9. On end: terminate Guacamole resources → `CLEAN_WORKSTATION` → release reservation → audit/archive  

## Authentication

- Portal authentication remains authoritative (session + CSRF for browser APIs).
- Guacamole admin credentials are server-only (`RemoteAnalysisSettings`).
- Users never log into Guacamole.
- Launch always originates from the Portal.

## Guacamole integration

Package: `iic_booking.remote_analysis.guacamole`

| Module | Role |
|--------|------|
| `client.py` | REST client (auth, connections, users) |
| `connection.py` | Ephemeral RDP connection builder |
| `session.py` | Session orchestrator |
| `services.py` | Facade + dashboard metrics |
| `cleanup.py` | Teardown + idle/expiry |
| `health.py` | Guacamole / agent / workstation health |
| `permissions.py` | Owner launch; admin terminate/observe |
| `views.py` | HTTP APIs |

`mock_guacamole=True` (default) runs without a live Guacamole server for development.

## Production Guacamole (Phase 2 / Workstream 2)

Existing session APIs are unchanged. Production turns off mock mode and points at a real gateway.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `RA_MOCK_GUACAMOLE` | `false` in production |
| `RA_GUACAMOLE_BASE_URL` | Public Guacamole URL (server-side redirects only) |
| `RA_GUACAMOLE_API_URL` | Internal REST API base (never returned to browsers) |
| `RA_GUACAMOLE_ADMIN_USERNAME` | Guacamole admin user |
| `RA_GUACAMOLE_ADMIN_PASSWORD` | Guacamole admin password |
| `RA_GUACAMOLE_DATA_SOURCE` | Usually `postgresql` |
| `RA_GUACAMOLE_VERIFY_TLS` | `true`/`false` for API TLS verify |
| `RA_APPLY_ENV_SETTINGS` | When `true`, persist env into `RemoteAnalysisSettings` on app ready |

`RemoteAnalysisSettings.get_solo()` applies env overlays automatically.

Bootstrap:

```bash
export RA_MOCK_GUACAMOLE=false
export RA_GUACAMOLE_API_URL=http://guacamole:8080/guacamole
export RA_GUACAMOLE_BASE_URL=https://guac.example.com/guacamole
export RA_GUACAMOLE_ADMIN_USERNAME=guacadmin
export RA_GUACAMOLE_ADMIN_PASSWORD='…'
python manage.py sync_remote_analysis_settings
```

Optional compose stack: `docker-compose.guacamole.yml` (guacd + guacamole + postgres).

Readiness probe `/api/v1/analysis/health/ready/` reports `checks.guacamole` as `ok` | `mock` | `unreachable` | `misconfigured…`.

Portal Guacamole REST client retries once on connection/5xx failures and re-authenticates once on HTTP 401.

## Security

Never returned to browsers:

- Windows username / password / RDP credentials  
- Workstation IP addresses  
- Guacamole admin credentials  
- Internal Guacamole API URLs  

Workstation IP removed from `AnalysisWorkstationSerializer`.  
RDP secrets live in `WorkstationRdpSecret` (encrypted with Fernet derived from `SECRET_KEY`).  
Session tokens are SHA-256 hashed, single-use, short-lived, optionally IP-bound.

## Cleanup

After session end:

1. Destroy Guacamole connection + temporary user  
2. Revoke unused tokens  
3. Queue `CLEAN_WORKSTATION`  
4. Mark workstation AVAILABLE  
5. Complete reservation when appropriate  
6. Write `SessionTermination` + `SessionStatistics` + audit  

Celery jobs:

- `advance_preparing_sessions`  
- `expire_desktop_sessions` (expiry + idle)  
- `monitor_session_health`  

## Failure recovery

| Failure | Handling |
|---------|----------|
| Guacamole unavailable | Fail session; mock mode for labs |
| Agent offline | Reject create; health score drops |
| Prepare timeout | Celery fails session |
| Token expiry / replay | Reject connect |
| Idle / session timeout | Automatic terminate + cleanup |
| Reservation expired | Reject create / expire open sessions |

## APIs

| Method | Path |
|--------|------|
| POST | `/api/v1/analysis/session/create/` |
| GET | `/api/v1/analysis/session/{id}/launch/` |
| GET | `/api/v1/analysis/session/{id}/connect/` |
| POST | `/api/v1/analysis/session/{id}/terminate/` |
| GET | `/api/v1/analysis/session/{id}/status/` |
| GET | `/api/v1/analysis/sessions/` |
| GET | `/api/v1/analysis/session/history/` |
| GET | `/api/v1/analysis/session/dashboard/` |

## Dashboard

Portal **Sessions** tab: active/idle/browser counts, connection health, timeline, current sessions, history, bandwidth, average duration.

## Future recording support

`SessionRecording` and `recording_enabled` exist as placeholders. Screen recording is **not** implemented in Milestone 4.

## Configuration

Admin singleton `RemoteAnalysisSettings`: Guacamole URLs, timeouts, clipboard/file/audio policies, display defaults, `mock_guacamole`.

Migration: `0003_browser_remote_desktop_guacamole`.
