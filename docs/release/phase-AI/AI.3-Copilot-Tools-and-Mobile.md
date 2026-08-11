# Phase AI — Research Copilot + Android Mobile

## Status (2026-08-10)

| Area | Status |
|------|--------|
| Research Copilot backend sources | Restored from stash + wired into `INSTALLED_APPS` / `/api/v1/research-copilot/` |
| Feature flag | `RESEARCH_COPILOT_ENABLED` default **false** (graceful 503 / bootstrap enabled=false) |
| Tools AI.3 | Read-only tools executable; mutating tools return portal action cards |
| RAG / Knowledge | Existing AI.2 engine restored |
| Frontend widget | Mounted (`ResearchCopilot`) + `apiClient.researchCopilot*` methods |
| Android app | `D:\IIC_NEW\iic-booking-android` (new) |
| Push devices | `PushDevice` model + `/api/notifications/devices/register/` |
| FCM send | Enabled when `FCM_SERVER_KEY` set; otherwise in-app/WebSocket + email unchanged |

## Architecture

```
User (Web / Android)
    ↓
Existing booking / sample / wallet / RA APIs
    ↓
Research Copilot (optional, flagged)
  - conversation + RAG + tools
  - action cards → portal routes (no permission bypass)
    ↓
CommunicationService
  - email
  - CommunicationLog push (notification center)
  - WebSocket
  - FCM (optional, registered PushDevice tokens)
```

## Enable Copilot (non-production first)

```bash
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_VERSION=0.1.0
OPENAI_API_KEY=...
RESEARCH_COPILOT_MODEL=gpt-4o-mini   # optional
```

Frontend:

```bash
VITE_RESEARCH_COPILOT_ENABLED=true
```

Migrations:

```bash
python manage.py migrate research_copilot
python manage.py migrate communication  # PushDevice 0053
python manage.py seed_research_copilot_knowledge  # optional
```

## Android

See `D:\IIC_NEW\iic-booking-android\README.md`.

Auth: DRF Token in EncryptedSharedPreferences.  
APIs reused: `/auth/login/`, `/bookings/`, `/equipments/`, `/notifications/`, `/v1/research-copilot/`.

## Tooling

```bash
pytest iic_booking/research_copilot/tests/ -q
pytest iic_booking/device_provisioning/tests/test_r26_compatibility.py -q
```
