# Production Release Checklist — Remote Analysis

Master gate before pilot/production enablement. Cross-check with `AdministratorChecklist.md`.

---

## Package contents

| Document | Path |
|----------|------|
| Release Notes | `Documentation/ReleaseNotes.md` |
| Deployment Guide | `Documentation/DeploymentGuide.md` + `PilotDeploymentGuide.md` |
| Rollback Guide | `Documentation/RollbackGuide.md` |
| Configuration | `configuration_catalog.py` + Release Notes env table |
| Known Limitations | Release Notes + `Phase2GapAnalysis.md` |
| Version Matrix | Release Notes |
| Migration order | Release Notes |
| Security | `SecurityAudit.md` |
| Performance | `PerformanceBenchmark.md` |
| UAT | `UserAcceptanceTest.md` |
| Failure sims | `FailureSimulation.md` |
| Audit | `ProductionAudit.md` |
| Cleanup | `CodeCleanupReport.md` |

---

## Pre-release verification (engineering)

- [x] Automated RA tests: **112 passed** (2026-07-30)  
- [x] Coverage: **90%** `iic_booking.remote_analysis`  
- [x] `makemigrations remote_analysis --check` clean  
- [x] Migrations 0001–0008 present; 0008 applied on validation DB  
- [x] `validate_remote_analysis` OK  
- [x] Agent Release build: **0 warnings / 0 errors**  
- [ ] Staging live Guacamole E2E (ops)  
- [ ] Staging performance sample (ops)  

---

## Configuration checklist

- [ ] `RA_MOCK_GUACAMOLE=false`  
- [ ] `RA_AGENT_ENROLLMENT_KEY` set (agents: `EnrollmentKey`)  
- [ ] Guacamole URLs + admin secrets set  
- [ ] `sync_remote_analysis_settings` run  
- [ ] Workspace roots + quotas set  
- [ ] Celery broker URL correct  
- [ ] Agent `PortalBaseUrl` HTTPS  
- [ ] Email backend configured if notifications required  

---

## Monitoring metrics — present vs recommended

### Already available (Operations Center)

| Metric | Status |
|--------|--------|
| Total / online / busy / available workstations | Present |
| Utilization / availability % | Present |
| Session & reservation success rates | Present |
| Queue length | Present |
| Open alerts | Present |
| Live workstation list + heartbeats | Present |
| Launch / prepare latency aggregates | Present |
| Portal latency from heartbeats | Present |

### Recommended additions (future — not blocking pilot)

| Metric | Why |
|--------|-----|
| Heartbeat staleness histogram (p50/p95) | Faster offline detection tuning |
| Cleanup failure rate (24h) | Spot dirty PCs |
| Agent version inventory card | Drift across 5 PCs |
| License / app inventory coverage | Software readiness |
| Session failure reason breakdown | Guacamole vs prep vs auth |
| Allocation time (queue wait → RESERVED) | SLA |

---

## Go / No-Go

| Gate | Go criteria |
|------|-------------|
| Security | Production gate in `SecurityAudit.md` closed |
| Functional | Admin checklist + UAT critical paths Pass |
| Ops | Backups + rollback drill documented |
| Guacamole | Readiness `ok` continuously for 24h soak |

**Sign-off:** ☐ GO  ☐ NO-GO — Owner: ________ Date: ________
