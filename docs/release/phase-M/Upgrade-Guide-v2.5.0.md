# Upgrade Guide — v2.5.0 Final

## From v2.5.0-rc24-release (or later RC)

1. Confirm nightly backup exists: `ls -lah /home/ubuntu/backups/nightly/latest/db/`  
2. Optional manual backup: `PG_MAJOR=17 /home/ubuntu/bin/iic-nightly-backup-cron.sh`  
3. Deploy tag:  
   `gh workflow run "Deploy Backend" -f release_tag=v2.5.0`  
4. Verify host: `cd /home/ubuntu/iic-booking-backend && git describe --tags`  
5. Smoke: portal 200, analysis ready, DSA/RA health  
6. Frontend: ensure compose healthcheck uses `http://127.0.0.1/health` and container is healthy  

## From earlier 2.5 RC / Phase L builds

1. Read Phase L certification and Known Issues.  
2. Apply DB migrations via normal Deploy Backend (migrate in deploy).  
3. Re-verify external sample Hold → Forward → Accept (requires ≥ rc24).  
4. Re-verify RA CLEAN clears BUSY (requires ≥ rc23).  

## Rollback

Redeploy previous tag via Deploy Backend. If migrations are incompatible, restore RDS snapshot / logical dump taken pre-upgrade, then redeploy matching code tag.

## Post-upgrade checklist

- [ ] Django healthy  
- [ ] Frontend healthy  
- [ ] Celery ping  
- [ ] Backup cron present  
- [ ] One DSA agent online  
- [ ] One Analysis PC AVAILABLE  
