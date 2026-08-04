# Phase 2.8 — Commit B1 proposed file set

**Commit:** B1 — Reverse Tunnel restoration  
**Proposed message title:** `feat(remote-analysis): restore reverse tunnel transport and orchestration`  
**Branch:** `feature/forward-port-reverse-tunnel`  
**Status:** Proposed — **not staged yet** (await confirmation)

## Include (whole file — tunnel-only)

| Path | Why |
|------|-----|
| `iic_booking/remote_analysis/tunnel.py` | Tunnel orchestrator / gateway client |
| `iic_booking/remote_analysis/tunnel_models.py` | TunnelSession / Event / Metric models |
| `iic_booking/remote_analysis/migrations/0017_restore_reverse_tunnel_transport.py` | Idempotent schema restore (0015 is empty stub on this tip) |
| `iic_booking/remote_analysis/tests/test_reverse_tunnel.py` | Tunnel tests |
| `iic_booking/remote_analysis/management/commands/verify_reverse_tunnel_production.py` | Production verify command |
| `iic_booking/remote_analysis/session_models.py` | `transport_mode` + tunnel gateway settings fields |
| `iic_booking/remote_analysis/guacamole/connection.py` | Provision/close tunnel on Guacamole connect |
| `iic_booking/remote_analysis/guacamole/settings_env.py` | `RA_TRANSPORT` / tunnel env overlays |
| `iic_booking/remote_analysis/configuration_catalog.py` | Catalog entries for tunnel settings |
| `iic_booking/remote_analysis/health.py` | Readiness checks for reverse_tunnel transport |
| `docs/ReverseTunnelArchitecture.md` | Docs |
| `docs/ReverseTunnelCommissioning.md` | Docs |
| `docs/ReverseTunnelSecurity.md` | Docs |
| `docs/ReverseTunnelTroubleshooting.md` | Docs |

## Include (partial — tunnel hunks only; must not take full WT)

| Path | B1 hunks only | Explicitly exclude |
|------|---------------|--------------------|
| `iic_booking/remote_analysis/models.py` | Import of `tunnel_models` (`TunnelSession`/`Event`/`Metric`) | `machine_guid` / `bios_uuid` / `machine_fingerprint` (→ B2) |
| `iic_booking/remote_analysis/constants.py` | `JOIN_TUNNEL`, `CLOSE_TUNNEL`, `TransportMode`, `TunnelSessionStatus` | Maintenance statuses, check-in enums (→ B2) |
| `iic_booking/remote_analysis/admin.py` | Settings transport fields + Tunnel* admin | ReservationQueue maintenance admin fields (→ B2) |
| `iic_booking/remote_analysis/services/commands.py` | `JOIN_TUNNEL` completion → `TunnelOrchestrator.apply_join_result` | Prefer also deferring non-tunnel prepare/`on_commit` refactor to B10 unless required to compile |

## Exclude from B1 (belong later)

| Path / area | Goes to |
|-------------|---------|
| RA migrations `0018`–`0020`, checkin/maintenance/fleet/commissioning services & tests | B2 |
| Equipment `0182`–`0184`, guacamole `session.py` duration | B3 |
| `deployment/`, lab, sync templates, installer discover urls | B4–B8 |
| Phase docs trees, `Documentation/*` commissioning guides | B8/B9 |
| `guacamole/authorization.py` (`AWAITING_CHECKIN`) | B2 |
| `guacamole/cleanup.py` maintenance/check-in release changes | B2 |
| `urls.py` / `views.py` fleet/maintenance/updates routes | B2 / B8 |
| `tmp_commission_run.py` | Never — clear from index as hygiene, not part of B1 |
| `__pycache__`, binaries | Never |

## Pre-stage hygiene (before `git add` for B1)

1. `git reset` (unstage current mixed index) — keeps working tree  
2. Remove `tmp_commission_run.py` from index (`AD` state)  
3. Stage only the B1 set above (partial files via B1-only content in index, full WT restored for remaining work)

## Validation before commit (after your OK to stage)

- `git diff --cached --name-only` matches this list  
- Cached `models.py` has **no** `machine_fingerprint`  
- Cached `constants.py` has **no** `MaintenanceKind`  
- No `bin/`/`obj/`/`tmp_*`
