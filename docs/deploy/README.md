# Deployment & operations docs

| Document | Audience |
|----------|----------|
| [../release/Backend-EC2-SSH-Deploy.md](../release/Backend-EC2-SSH-Deploy.md) | **Active** GitHub → SSH → EC2 release deploy |
| [Production-Deployment-Guide.md](Production-Deployment-Guide.md) | Fresh install, upgrade, rollback, DR |
| [Operations-Runbook-IITR.md](Operations-Runbook-IITR.md) | Day-2 ops for IIT Roorkee admins |
| [MONITORING.md](MONITORING.md) | Health / readiness / metrics URLs |
| [AGENT_INSTALL.md](AGENT_INSTALL.md) | Windows Analysis PC agent |

**Scripts (repo root):** `deploy.sh`, `rollback.sh`, `verify-production.sh`  
**Scripts (`scripts/deploy/`):** `backup.sh`, `restore-verify.sh`, `validate-startup.sh`, `lib.sh`

**Compose:** `docker-compose.production.yml` (Deploy Backend default) · `docker-compose.ra-production.yml` (full RA stack)
