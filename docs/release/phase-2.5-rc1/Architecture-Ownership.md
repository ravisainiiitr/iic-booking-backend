# Architecture Ownership - Phase 2.5 RC1

This document is the architectural ownership map for RC1 commit construction.

## Backend ownership map

| Commit ID | SHA | Architectural capability owned | Major modules | Public APIs added/changed | Database migrations | Documentation added/updated | Tests included | Depends on previous commits | Enables subsequent commits | Integration points | Operational impact | Future maintenance notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B1 | `d4d50e29891bce543d6d9258958fb744df71d90e` | Reverse Tunnel transport restoration and orchestration | `iic_booking/remote_analysis/tunnel.py`, `iic_booking/remote_analysis/tunnel_models.py`, tunnel wiring in guacamole/session lifecycle | Reverse tunnel join/close transport paths under existing analysis APIs | `remote_analysis/0017_restore_reverse_tunnel_transport.py` | Reverse tunnel architecture, commissioning, security, troubleshooting docs | `iic_booking/remote_analysis/tests/test_reverse_tunnel.py` | Baseline branch tip before Phase 2.8 controlled history | B2 session execution engine can rely on tunnel lifecycle and statuses | Guacamole integration, command completion callbacks, scheduler/cleanup flow | Restores production-safe remote desktop connectivity over tunnel transport | Keep transport and tunnel-state transitions backward-compatible with existing agent behavior |
| B2 | `500629b60992839fce99be2d2257230dfcb43ba3` | Remote Analysis execution engine (lifecycle + reservation + allocation + workspace orchestration) | `iic_booking/remote_analysis/**`, `iic_booking/equipment/remote_analysis_integration/**`, equipment RA fields/migrations | Booking analysis lifecycle APIs (`start/release/end/extend/files/upload`) and execution lifecycle internals | Equipment RA migrations `0182-0184`, remote analysis reservation/check-in migration(s) included in commit | `docs/RemoteAnalysisSessionLifecycle.md` plus RA operational guides updates | Session lifecycle and allocation tests included with subsystem changes | B1 reverse tunnel restoration | B3 Deployment Center, B4 Plug-and-Play, B5 Lab Infrastructure onward | Booking domain, Guacamole, workspace sync/transfer, scheduler/availability, equipment configuration | Establishes full executable remote-analysis flow for RC1 capability boundary | Future commits should treat this as the authoritative execution core and avoid cross-cutting rewrites without explicit boundary justification |
| B3 | `24fb089613ad7fd51dd39bde24ebf1f2845a385d` | Deployment Center backend and installer distribution control plane | `iic_booking/deployment/**`, installer ticket routing, deployment app registration | Deployment release catalog, wizard/download endpoints, compatibility and repair package APIs | Deployment migrations `0001-0002` | Deployment center and installer/release runbook updates | Deployment command coverage and API smoke tests where present | B2 complete RA execution engine | B4/B5+ consume release/distribution metadata | Portal auth, ticketing, installer distribution, release policy metadata | Introduces centralized installer lifecycle and controlled agent/wizard distribution | Keep installer compatibility schema stable for external automation and RC reproducibility |
| B4 | `TBD (assigned after commit)` | Plug-and-Play equipment onboarding, discovery, and config-push integration | `iic_booking/sync/**`, sync admin templates and provisioning endpoints | Sync template, IP reservation, discovery/provisioning and configuration push APIs | Sync migrations `0017-0018` | Plug-and-Play architecture and operations docs | Sync bootstrap/heartbeat/provisioning tests where present | B2 and B3 | B5 Lab Infrastructure, B6 Diagnostics and Reporting | DSA heartbeat/bootstrap paths, equipment inventory, deployment compatibility links | Standardizes onboarding workflow and configuration delivery | Keep template and reservation schema backward-compatible with existing DSA payloads |

## Planned next backend ownership

| Commit ID | Capability |
|---|---|
| B3 | Deployment Center |
| B4 | Plug-and-Play Platform |
| B5 | Laboratory Infrastructure |
| B6 | Diagnostics and Reporting |
| B7 | SAT Dashboard |
| B8 | Cross-cutting Stabilization |

