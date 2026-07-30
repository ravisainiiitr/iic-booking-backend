# Operations Runbook — Remote Analysis (IIT Roorkee)

**Audience:** On-call / system administrators  
**Companion:** [Production-Deployment-Guide.md](Production-Deployment-Guide.md)

## Daily

1. `curl -fsS https://<portal>/api/v1/analysis/health/ready/` → `status=ready`  
2. Toolkit HTML (manage users): workstation online count, Guacamole tab  
3. Disk: Docker volumes `production_media`, `production_postgres`  
4. Celery: Flower or `docker compose … logs celeryworker --tail=50`  

## Deploy / upgrade

```bash
cd /opt/iic-booking-backend   # or your path
./deploy.sh
```

On FAIL from `validate_deployment_startup`, **do not** force traffic. Fix env and re-run.

## Rollback

```bash
./rollback.sh
```

If readiness still fails after rollback, restore DB:

```bash
gunzip -c backups/deploy/<label>/db/portal.sql.gz \
  | docker compose -f docker-compose.ra-production.yml exec -T postgres \
    sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

## Backup

```bash
./scripts/deploy/backup.sh --label "ops-$(date -u +%Y%m%d)"
./scripts/deploy/restore-verify.sh backups/deploy/ops-YYYYMMDD
```

Cron example (02:30 IST):

```cron
30 2 * * * cd /opt/iic-booking-backend && ./scripts/deploy/backup.sh --label "nightly-$(date -u +\%Y\%m\%d)" >>/var/log/ra-backup.log 2>&1
```

## Incident: Portal down

1. `docker compose -f docker-compose.ra-production.yml ps`  
2. `logs django --tail=200`  
3. Ready probe JSON — note failing check  
4. Redis/Postgres health  
5. Redeploy or rollback  

## Incident: Guacamole down

1. `docker compose … --profile guacamole ps`  
2. Portal readiness `guacamole` field  
3. Restart `guacamole` `guacd` `guacamole-db`  
4. Sync path (file transfer) can continue without Guacamole  

## Incident: Agents offline

1. Not a Portal compose issue — check PC service + network to Portal  
2. Toolkit agent diagnostics  
3. Re-enroll if token lost  

## Verification commands

```bash
./verify-production.sh
ADMIN_TOKEN=… RUN_CONNECTIVITY=1 ./verify-production.sh
./scripts/deploy/validate-startup.sh
./scripts/HealthCheck.sh https://<portal> <token>
```

## Contacts / ownership

| Area | Owner |
|------|-------|
| Portal / Docker / TLS | Institute IT / Portal admins |
| Analysis PCs / Agent | Lab engineers |
| Guacamole / RDP accounts | Lab + IT jointly |

## References

- RC1 pack: `docs/release/rc1/`  
- Guacamole runbook: `docs/RemoteAnalysisGuacamoleRunbook.md`  
- Monitoring: `docs/deploy/MONITORING.md`  
- Troubleshooting: `docs/release/rc1/08-Troubleshooting-Guide.md`  
