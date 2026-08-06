# Go-Live Checklist — v2.5.0 Final

## Pre-cutover

- [x] Phase L qualification PASS (conditional GO)  
- [x] Nightly RDS backup installed and first run PASS  
- [x] Restore-verify with temp DB PASS  
- [x] Root disk &lt; 70% (achieved ~39%)  
- [x] Frontend container healthcheck healthy  
- [ ] Communications to departments / operators / faculty  
- [ ] Support roster published  
- [ ] Maintenance window announced (if any)  

## Cutover

- [ ] Tag `v2.5.0` (Final) deployed via Deploy Backend  
- [ ] `git describe` on host matches `v2.5.0`  
- [ ] Smoke: login, book, sample, DSA, RA, invoice  
- [ ] Confirm cron: `crontab -l | grep iic-nightly`  
- [ ] Confirm agents online  

## Post-cutover (first 48h)

- [ ] Watch backup log next night  
- [ ] Monitor disk, Celery, Guacamole memory  
- [ ] Triage user tickets; track KI-* known issues  
- [ ] Daily admin checklist executed  

## Sign-off

| Role | Name | Date |
|------|------|------|
| Release engineer | | |
| Lab / ops lead | | |
| Department sponsor | | |
