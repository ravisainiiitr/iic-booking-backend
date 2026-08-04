# Change Log — Platform 2.5.0-rc1 (Draft)

Format: Keep a Changelog. Entries finalized when commits exist.

## [2.5.0-rc1] — TBD

### Added

- DSA D4: release architecture and operations documentation pack, pilot acceptance runbooks, deployment/packaging/security/troubleshooting guides, and integration test workflow collateral
- DSA D3: monitoring and diagnostics platform with heartbeat/health services, log and system-health APIs, telemetry collectors, and monitoring dashboard UI
- DSA D2: configuration platform including synchronization engine, upload/result-processing pipelines, offline recovery workflows, and persistence migration chain
- DSA D1: discovery and provisioning control plane with enrollment APIs, equipment discovery/provisioning services, and provisioning UI surfaces
- DSA D0: repository recovery baseline with normalized solution structure, shared infrastructure foundations, installer/wizard infrastructure, and project integrity repairs
- RAA R4: installer project/assets, enrollment key operational scripts, and release documentation for packaging and operations handoff
- RAA R3: workspace maintenance lifecycle updates for session execution cleanup/synchronization behavior
- RAA R2: startup diagnostics plus heartbeat and reverse-tunnel hardening for connectivity resilience
- RAA R1: repository foundation and enrollment bootstrap hardening across startup configuration, program wiring, and persistent agent state handling
- Frontend F4: SAT execution dashboard with guided test runs, evidence capture, readiness scoring, diagnostics/utilization surfaces, and CSV/Excel/PDF report exports
- Frontend F3: Laboratory Infrastructure fleet dashboard, node diagnostics/repair controls, alerts/audit/compliance views, and RDP path diagnostics UI
- Frontend F2: Deployment Center capability with installer version cards, secure ticketed downloads, compatibility metadata, and dashboard/routing integration for Plug-and-Play operations
- Frontend F1: Analyze Data launch page now surfaces backend failure category and user-facing launch error details during PREPARE/session lifecycle flow
- Cross-cutting stabilization collateral for release governance, operational checklists, and commit-process traceability artifacts
- SAT dashboard and acceptance/readiness documentation set for execution evidence and release-gate traceability
- Diagnostics and reporting documentation pack for operational troubleshooting, readiness, and compliance-oriented workflows
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
