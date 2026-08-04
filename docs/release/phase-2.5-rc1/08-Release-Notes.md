# Release Notes — Platform 2.5.0-rc1 (Draft)

**Product:** IIC Laboratory Platform  
**Version:** 2.5.0-rc1  
**Date:** TBD  
**Commits:** TBD (see Manifest)

## Highlights

- **Phase 1 Plug-and-Play:** Deployment Center, DSA discovery/pairing, Equipment PC Wizard, config templates, soft IP, RAA enrollment/link.  
- **Phase 2 Enterprise Lifecycle:** Laboratory Infrastructure fleet UI/API, config push/ack, alerts, repair/diagnostics, software compliance, utilization reporting.  
- **Phase 2.5 Stabilization + Lab SAT:** Critical defect fixes; Acceptance Test / SAT Execution Dashboard (wizard, evidence, defects, readiness, reports).

## Control planes (unchanged)

- Portal → DSA → Equipment PC  
- Portal → RAA → Analysis PC  

## Upgrade impact

- Requires database migrations (see Upgrade Guide).  
- Frontend 2.5 requires Backend 2.5 for Lab / Deployment Center / SAT pages.  
- Agents should be upgraded via Deployment Center after Portal is live.

## Breaking / notable

- DSA pairing **requires** `LocalApi:ManagementApiKey` (fail-closed).  
- RA `0017` reverse-tunnel restore migration — verify on staging before prod.

## Related docs

- Known Issues, Upgrade Guide, Rollback Plan in this folder.  
- Lab SAT: `docs/phase-2.5/`.  
- Prior RA-only RC: `docs/release/rc1/` (1.0.0-rc1) — separate stream.
