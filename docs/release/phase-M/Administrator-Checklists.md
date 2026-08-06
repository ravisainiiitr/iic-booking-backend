# Administrator Checklists — v2.5.0

## Daily

- [ ] Portal home HTTPS 200  
- [ ] `/api/v1/analysis/health/ready/` ready  
- [ ] Django + frontend containers **healthy**  
- [ ] Celery worker ping OK  
- [ ] Disk `/` &lt; 70%  
- [ ] Nightly backup log shows last night `PASS` (`/var/log/iic-nightly-backup.log`)  
- [ ] At least one DSA agent online (heartbeat &lt; 60s)  
- [ ] Analysis PC idle status AVAILABLE (or known busy with active session)  
- [ ] No P1 tickets open without owner  

## Weekly

- [ ] `VERIFY_RESTORE_DB=1` restore-verify on `backups/nightly/latest`  
- [ ] Review Celery / Flower failed tasks  
- [ ] Review DSA upload failures / stuck Queued  
- [ ] Review RA command history failures  
- [ ] `docker image prune` / builder prune if disk &gt; 60%  
- [ ] Confirm Guacamole + reverse-tunnel gateway healthy  
- [ ] Spot-check invoice PDF + wallet for one completed booking  
- [ ] Review AWS RDS CPU/storage/connections alarms  

## Monthly

- [ ] Full smoke: internal book → sample accept → DSA result → complete → invoice  
- [ ] External smoke: Hold → Forward → Accept → result (FBR note for user download)  
- [ ] RA analyze → start → desktop launcher → end on completed booking  
- [ ] Rotate / review admin and SAT credentials as policy requires  
- [ ] Audit enabled workstations / disable archived agents  
- [ ] Review backup retention (14-day nightly + RDS snapshots)  
- [ ] Review security group / TLS cert expiry  

## Quarterly

- [ ] Disaster-recovery tabletop: restore RDS snapshot or logical dump to staging  
- [ ] Server reboot drill (or documented deferral with restart-policy evidence)  
- [ ] Capacity review: disk, Guacamole memory, Analysis PC count  
- [ ] Dependency / CVE review for base images  
- [ ] Update onboarding docs if workflows changed  
- [ ] Re-issue Phase L-style sample of security checks (unauth 401, IDOR 403)  
