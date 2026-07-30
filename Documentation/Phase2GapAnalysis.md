# Phase 2 Gap Analysis — Remote Analysis Production Integration

Updated: 2026-07-30

Final audit after Workstreams 1–4. Scope: fill production gaps without rewriting existing RA modules, DSA, or the booking portal core.

## Already satisfied (pre–Phase 2 or earlier milestones)

| Area | Evidence |
|------|----------|
| Workstation pool / control plane | Models, agent APIs, commands |
| Scheduler / allocation / queue | `SchedulerService`, Celery beat tasks |
| Guacamole orchestrator + browser APIs | Mock-capable session lifecycle |
| Booking entitlement | Reservation ↔ booking hooks |
| Frontend `/remote-analysis` | Existing SPA routes |
| Ops / collaboration / workspace | Packages M5–M7 |
| RBAC + agent auth | DRF permissions / agent token |
| Health probes | `/health/`, `/live/`, `/ready/` |
| DB indexes (M8) | Migration `0007_*` |
| Celery retries | `ra_periodic_task` |
| Agent HTTP retries / re-register | Portal client in RAA |
| Docs baseline | Architecture, deployment, ops, DR, testing |

## Implemented in Phase 2

| Workstream | Deliverable |
|------------|-------------|
| WS1 | Windows `RemoteAnalysisAgent` (register, heartbeat, inventory, commands, prepare/cleanup, workspace sync) |
| WS2 | Production Guacamole env overlays (`RA_*`), sync command, compose Guacamole stack, readiness Guacamole check |
| WS3 | Automated tests (~107+) with **≥90%** line coverage on `iic_booking.remote_analysis`; production fixes found in testing |
| WS4 | Hardening wire-up (below) + docs + this gap analysis |

### WS4 hardening patches

1. **Correlation IDs** — `RemoteAnalysisCorrelationMiddleware` + structured logs on key Celery jobs  
2. **Agent local health** — loopback `GET /api/health` via `LocalHealthPort` (default 5088)  
3. **Configuration catalog** — `RA_*` keys; Agent `PortalBaseUrl` / `LocalHealthPort` (removed stale `PortalUrl` / `LocalApiPort`)  
4. **Guacamole client** — one transient retry + re-auth on 401  
5. **Ops** — Compose django healthcheck on readiness; index `(status, last_heartbeat)`; list APIs use `parse_pagination`

## Remaining limitations (document-only / ops / future)

| Item | Notes |
|------|-------|
| Session recording | Placeholder / stub — not a production recorder |
| Virus scanner | `noop` unless an implementation is plugged in |
| Notification channels | Some collaboration channels remain stubs |
| Agent TFM | Built as `net10.0-windows` (SDK 10 available; spec mentioned .NET 9) |
| Live Guacamole | Requires deployed Guacamole + `RA_MOCK_GUACAMOLE=false` + secrets |
| Frontend E2E / load tests | Not in backend package coverage |
| Production Django hardening | DEBUG off, TLS/HSTS, secret rotation — ops checklist |
| Full browser RDP E2E on real hardware | Manual acceptance after Guacamole go-live |

## Acceptance checklist (Phase 2)

| Criterion | Status |
|-----------|--------|
| Agent operational, auto-register, heartbeats | ✅ WS1 |
| Live Guacamole can replace mock | ✅ WS2 (ops gate) |
| Browser RDP E2E (mock path automated) | ✅ WS3; live path ops |
| Prepare / cleanup commands | ✅ WS1 |
| Healthy scheduler allocation | ✅ pre-existing + tests WS3 |
| Backward compatible with booking portal / DSA | ✅ no rewrites of those systems |
| Tests / coverage ≥90% | ✅ WS3 |
| Docs updated | ✅ WS2–WS4 |
| Production hardening checklist addressed or documented | ✅ WS4 |

## Non-goals respected

- No duplicate schedulers, models, or public API redesigns  
- No DSA / DepartmentSyncAgent merge  
- Small additive changes only
