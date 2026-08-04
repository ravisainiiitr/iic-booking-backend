# DSA Architecture Ownership - Phase 3

## Commit ownership map

| Commit ID | SHA | Capability | Primary ownership |
|---|---|---|---|
| D0 | `b657c20228a9c7f273d78c0af6c6b25e059fa1f7` | Repository Recovery | Repository normalization baseline, shared infrastructure/configuration, common models/DTOs, dependency/build/installer integrity |
| D1 | `f58f8e5937c4f8e117d1af14b5e9ae01c9757b4e` | Discovery & Provisioning | Enrollment workflows, equipment discovery/provisioning services, discovery/provisioning APIs and UI surfaces |
| D2 | `6c0191f1c7187ce005756264d9aa209c11546213` | Configuration Platform | Configuration synchronization, upload engine, result processing, offline recovery, migration chain for persistence and queue state |
| D3 | `6d9e5dd52ac80ceb564d947fba3fe16082e11224` | Monitoring Platform | Health checks, heartbeat diagnostics, monitoring telemetry services, logs/system-health APIs and dashboards |
| D4 | `495e27b56377b1168328189ad82f2bfeee2be826` | Documentation & Release | Architecture/deployment/operations/troubleshooting/pilot/release documentation and CI workflow collateral |

## Ownership validation

- D0 contains repository-wide unsplittable recovery changes.
- D1-D4 are capability-scoped and build on D0 in dependency order.
- No overlaps requiring additional normalization were identified after D0 commit creation.
