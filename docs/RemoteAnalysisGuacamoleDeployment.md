# Guacamole Deployment Guide

## Components

- Portal (Django) with Phase 3 Guacamole package  
- Apache Guacamole + guacd (+ Guacamole DB) — see `docker-compose.guacamole.yml`  
- Analysis PCs reachable **from guacd** on RDP (usually 3389)  
- Remote Analysis Agent on each Analysis PC  

## Bootstrap

```bash
export RA_MOCK_GUACAMOLE=false
export RA_GUACAMOLE_API_URL=http://guacamole:8080/guacamole
export RA_GUACAMOLE_BASE_URL=https://guac.example.com/guacamole
export RA_GUACAMOLE_ADMIN_USERNAME=guacadmin
export RA_GUACAMOLE_ADMIN_PASSWORD='…'
export RA_GUACAMOLE_DATA_SOURCE=postgresql
export RA_GUACAMOLE_VERIFY_TLS=true
export RA_APPLY_ENV_SETTINGS=true
python manage.py migrate
python manage.py sync_remote_analysis_settings
```

Optional stack:

```bash
docker compose -f docker-compose.guacamole.yml up -d
```

Default compose maps host **8085 → 8080** on the Guacamole container.

## Workstation RDP secrets

In Django admin, set `WorkstationRdpSecret` per Analysis workstation (username/password/domain/port).  
Portal never returns these to browsers.

## HTTPS

- Terminate TLS at the reverse proxy for Portal and Guacamole public URLs.  
- Set `RA_GUACAMOLE_BASE_URL` to the **public** HTTPS Guacamole URL (used only for browser redirects).  
- Set `RA_GUACAMOLE_API_URL` to the **internal** API base (never returned to browsers).  
- Keep `verify_tls=True` unless using a lab cert explicitly.

## Health

- Ready probe: `/api/v1/analysis/health/ready/` → `checks.guacamole`  
- Toolkit: `/api/v1/analysis/operations/toolkit/?view=html` → Guacamole tab  

## Rollback

1. Set `RA_MOCK_GUACAMOLE=true` (or Admin → Remote Analysis Settings → mock on)  
2. Run `python manage.py sync_remote_analysis_settings` if using env overlay  
3. Optionally stop Guacamole compose stack  
4. Existing sync / commissioning workflows continue without Guacamole  

Session rows remain in DB; open sessions can be terminated via  
`POST /api/v1/analysis/session/{id}/terminate/` or Celery idle/expiry cleanup.

## Related

- [RemoteAnalysisGuacamoleConfiguration.md](RemoteAnalysisGuacamoleConfiguration.md)  
- [RemoteAnalysisGuacamoleRunbook.md](RemoteAnalysisGuacamoleRunbook.md)  
- [RemoteAnalysisGuacamoleSecurity.md](RemoteAnalysisGuacamoleSecurity.md)  
