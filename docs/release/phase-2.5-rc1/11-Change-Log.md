# Change Log — Platform 2.5.0-rc1 (Draft)

Format: Keep a Changelog. Entries finalized when commits exist.

## [2.5.0-rc1] — TBD

### Added

- Laboratory Infrastructure backend foundation for fleet operations, heartbeat health monitoring, maintenance orchestration, and operational alerting workflows
- Plug-and-Play backend foundation: sync templates, IP reservation workflow, and DSA bootstrap/config integration endpoints
- Deployment Center backend module for release catalog, compatibility metadata, and installer distribution
- Equipment PC Wizard installer publishing and ticketed download path integration
- Remote Analysis execution engine lifecycle APIs and orchestration:
  session state machine, PREPARE/COLLECT flow, End Analysis, Extend Analysis, Upload Past Data, cleanup/timeouts, and reservation lifecycle handling
- Equipment Remote Analysis configuration surface:
  default duration, extension, RAW/RESULTS directories, and check-in policy fields with migrations
- Software-aware workstation allocation and capability filtering in reservation flow
- Session lifecycle reference:
  `docs/RemoteAnalysisSessionLifecycle.md`
- Laboratory Infrastructure APIs and Main Admin fleet UI  
- Deployment Center (DSA / RAA / Wizard distribution)  
- Lab SAT Execution Dashboard (runs, evidence, defects, reports, readiness)  
- Equipment sync templates, soft IP reservation (portal)  
- DSA Equipment PC discovery / pairing / config-pack / status rollup (agent)  
- Equipment PC Configuration Wizard  
- Config ack, lab alerts, repair/diagnostics, software compliance, utilization CSV  
- Phase 2.5 / enterprise / plug-and-play documentation sets  

### Changed

- Heartbeat ingest accepts DSA `equipment_pcs`  
- Config push persists full profile fields  
- RAA update discover/report auth for agents  
- Pairing fail-closed without ManagementApiKey  
- OTP not stored in DSA ConfigJson  

### Fixed

- Phase 2.5 Critical/High items C-01, C-02, H-01, H-02, H-04, H-05, H-07, H-08, H-09, H-12 (see Production-Readiness Phase 2.5 doc)

### Security

- Loopback EqPC status requires management key or pairing token  

### Removed

- None intentional for RC1  

---

## [Unreleased] working tree

Not yet in git history — see Deployment Audit 2026-08-04.
