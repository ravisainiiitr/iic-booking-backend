# Release Notes — v2.5.0 Final

**Date:** 2026-08-06  
**Base:** `v2.5.0-rc24-release` (`b3bf95c`)  
**Portal:** https://equip.iitr.ac.in  

## Highlights

- Institute production rollout of Equipment Booking Portal with Department Sync Agent and Remote Analysis (reverse-tunnel Guacamole).  
- Phase L go-live qualification completed (conditional GO).  
- Phase M.1 operational hardening: nightly RDS backups, verified restore, disk cleanup, frontend healthcheck fix.

## Fixes included since RC train

| Release | Fix |
|---------|-----|
| rc22 | Booking `set_rollback` outside atomic → HTTP 500 |
| rc23 | Remote Analysis sticky BUSY after CLEAN |
| rc24 | External sample accept after Hold/Forward |
| Final ops | Frontend healthcheck IPv4 (`127.0.0.1/health`); nightly backup + restore-verify |

## Components

- Backend portal (Django / Celery / Redis)  
- Frontend (nginx SPA)  
- DSA Windows agent  
- Remote Analysis Agent + Guacamole gateway  

## Compatibility

- PostgreSQL 17 (RDS)  
- Docker Compose production stack on EC2  
- Windows Server/10+ for DSA and RAA  

## Documentation

See `docs/release/phase-M/` and `docs/release/phase-L/PRODUCTION-READINESS-CERTIFICATION.md`.
