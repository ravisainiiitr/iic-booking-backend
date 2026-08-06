# Phase M.1 — Operational Hardening Record

**Date:** 2026-08-06  
**Release:** v2.5.0 Final Candidate → Final  

## Results

| Task | Result | Evidence |
|------|--------|----------|
| Nightly DB backups | PASS | Cron `30 2 * * * TZ=Asia/Kolkata`; `/home/ubuntu/bin/iic-nightly-backup-cron.sh`; dump `8.1M` at `/home/ubuntu/backups/nightly/nightly-20260806` |
| Verified restore | PASS | `VERIFY_RESTORE_DB=1` created/restored/dropped `iic_restore_verify_*` on RDS PG17 |
| Root disk &lt;70% | PASS | 80% → **39%** after docker builder/image prune + journal vacuum |
| Frontend health | PASS | Root cause: `localhost`→IPv6; fixed to `127.0.0.1/health`; container **healthy**; site 200 |

## No production blockers

Hardening completed without application defects requiring code freeze.

## Artifacts

- Ops scripts: `scripts/ops/iic-nightly-backup.sh`, `scripts/ops/iic-restore-verify.sh`  
- Docs: `docs/release/phase-M/*`  
- Frontend compose fix: `iic-booking-frontend/docker-compose.production.yml` (local + production)  
