# Guacamole Configuration Guide

## RemoteAnalysisSettings (singleton)

| Field | Purpose | Default |
|-------|---------|---------|
| `mock_guacamole` | In-process mock (dev/test) | `True` |
| `guacamole_base_url` | Public Guacamole URL for redirects | empty |
| `guacamole_api_url` | Internal REST API base | empty |
| `guacamole_admin_username` / `password` | Server-only Guac admin | empty |
| `guacamole_data_source` | Guac auth data source | `postgresql` |
| `verify_tls` | Verify Guac API TLS | `True` |
| `connection_timeout` | Guac API timeout (s) | 30 |
| `session_timeout` | Max session duration (min) | 120 |
| `idle_timeout` | Idle disconnect (min) | 15 |
| `max_concurrent_sessions` | Global open session cap | 50 |
| `single_active_session_per_booking` | One open session per booking | `True` |
| `clipboard_enabled` / `clipboard_policy` | Clipboard | on / text |
| `file_transfer_enabled` / policy | Guac drive / upload-download | off / disabled |
| `audio_enabled` | RDP audio | on |
| `recording_enabled` | Reserved — not implemented | off |
| `launch_token_lifetime_seconds` | One-time Portal token TTL | 90 |
| `bind_token_to_ip` | Bind launch token to client IP | False |
| `prepare_timeout_seconds` | Agent prepare wait | 120 |

## Environment overlays

| Variable | Maps to |
|----------|---------|
| `RA_MOCK_GUACAMOLE` | `mock_guacamole` |
| `RA_GUACAMOLE_BASE_URL` | `guacamole_base_url` |
| `RA_GUACAMOLE_API_URL` | `guacamole_api_url` |
| `RA_GUACAMOLE_ADMIN_USERNAME` | admin user |
| `RA_GUACAMOLE_ADMIN_PASSWORD` | admin password |
| `RA_GUACAMOLE_DATA_SOURCE` | data source |
| `RA_GUACAMOLE_VERIFY_TLS` | `verify_tls` |
| `RA_APPLY_ENV_SETTINGS` | persist env into DB on ready |

Command: `python manage.py sync_remote_analysis_settings`

## Connection policies mapped to Guacamole RDP params

Built in `guacamole/connection.py`:

- Clipboard → `disable-copy` / `disable-paste`  
- File transfer / drive → `enable-drive`, `disable-upload` / `disable-download`  
- Audio → `enable-audio` / `disable-audio`  
- Printing → disabled (`enable-printing=false`, `disable-print=true`)  

## Launcher URLs

| URL | Role |
|-----|------|
| `/api/v1/bookings/{id}/analysis/desktop/?view=html` | HTML launcher |
| `/api/v1/bookings/{id}/analysis/launch/` | Create/reuse session + `launch_url` |
| `/api/v1/analysis/session/{id}/launch/` | Issue one-time Portal token |
| `/api/v1/analysis/session/{id}/connect/?t=…&redirect=1` | Consume token → Guac redirect |
