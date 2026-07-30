# Remote Analysis RC1 — Production Checklist

Use this as the final go-live gate. Check every box before declaring production service.

## Database

- [ ] PostgreSQL HA/backup verified  
- [ ] `migrate remote_analysis` through **0012**  
- [ ] `showmigrations` clean  
- [ ] Backup job includes RA tables  

## Redis

- [ ] Redis reachable  
- [ ] Used as Celery broker  
- [ ] Cache probe OK or documented degraded mode  

## Celery

- [ ] Worker running  
- [ ] Beat running (single leader)  
- [ ] RA periodic tasks present (expire/advance/cleanup/health/…)  

## Portal

- [ ] `DEBUG=False`  
- [ ] `SECRET_KEY` set and backed up  
- [ ] TLS on public Portal URL  
- [ ] `/health/live/` OK  
- [ ] `/health/ready/` → `ready`  
- [ ] `RA_AGENT_ENROLLMENT_KEY` set  
- [ ] `RA_MOCK_GUACAMOLE=false`  

## Agent

- [ ] Service installed on target PC(s)  
- [ ] Enrollment successful  
- [ ] Heartbeat &lt; 90s  
- [ ] Prepare/clean smoke OK  

## Guacamole

- [ ] guacd + guacamole + DB up  
- [ ] Public HTTPS base URL  
- [ ] Internal API URL from Portal  
- [ ] Admin creds in settings  
- [ ] RDP secrets per workstation  
- [ ] guacd → PC:3389 open  
- [ ] Desktop launch redirect works  

## TLS

- [ ] Portal HTTPS  
- [ ] Guacamole HTTPS  
- [ ] Agent trusts Portal cert chain  
- [ ] `RA_GUACAMOLE_VERIFY_TLS` appropriate  

## Backups

- [ ] DB backup tested  
- [ ] Media/workspace backup tested  
- [ ] Secret backup process documented  
- [ ] Restore drill scheduled  

## Monitoring

- [ ] Readiness scraped / alerted  
- [ ] Toolkit Guacamole + workstation offline alerts  
- [ ] Disk free space on Portal media + Analysis PCs  

## SAT

- [ ] Automated `sat` suite green on RC build  
- [ ] Lab SAT for first workstation (as required)  
- [ ] Guacamole SAT-11 mock green; live as required  

## Commissioning

- [ ] Phase 2 live commissioning report completed for first PC  
- [ ] Evidence ZIP retained  
- [ ] Equipment flag enabled only after PASS  

## Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Administrator | | | |
| Lab Engineer | | | |
| Product / Release owner | | | |
