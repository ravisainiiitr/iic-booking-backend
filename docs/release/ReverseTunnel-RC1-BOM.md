# Reverse Tunnel RC1 — Bill of Materials (BOM)

**Document status:** Audit / planning only — no commit executed  
**Date:** 2026-07-31  
**Base Portal tip:** `ac70cfa61deb3554d4932be461db3ef77a5ea0c9` (`origin/master`)  
**RC commit:** `_PLACEHOLDER_`

---

## Portal

| Field | Value |
|-------|-------|
| Repository | `iic-booking-backend` (`ravisainiiitr/iic-booking-backend`) |
| Recommended branch | `release/reverse-tunnel-rc1` |
| Version / tag | `ReverseTunnel-RC1` (tag after commit) |
| Commit | `_PLACEHOLDER_` |
| Compose file | `docker-compose.ra-production.yml` |
| Django image | `iic_booking_production_django` (rebuild) |
| Migration | `remote_analysis.0015_reverse_tunnel_transport` |
| Default transport | `direct_rdp` / `RA_TRANSPORT=direct_rdp` |

## Gateway

| Field | Value |
|-------|-------|
| Repository / path | Sibling `../ReverseTunnelGateway` (not inside Portal git) |
| Version | Build from local tree; **no semver tag yet** — pin by git hash or archive at ship time |
| Target framework | `net10.0` |
| Docker image | Compose service `reverse-tunnel-gateway` (build context `../ReverseTunnelGateway`) |
| Listen | `0.0.0.0:7090` (`ASPNETCORE_URLS`) |
| Admin API | `api/v1/tunnels/allocate`, `…/close`, `api/v1/health`, `api/v1/metrics` |
| Agent endpoint | WSS join (token HMAC) |

## Agent

| Field | Value |
|-------|-------|
| Repository / path | `../RemoteAnalysisAgent` (not inside Portal git) |
| Version | `1.0.0` (`RemoteAnalysisAgent.csproj`) + tunnel handlers |
| Target framework | `net10.0-windows` |
| Required commands | `JOIN_TUNNEL`, `CLOSE_TUNNEL` |
| Deploy | Windows service / MSI — not Docker |

## Migration

| App | Name | Depends on |
|-----|------|------------|
| `remote_analysis` | `0015_reverse_tunnel_transport` | `0014_analysis_workflows`, `equipment.0181` |

Creates/updates: `RemoteAnalysisSettings` tunnel fields; `TunnelSession`, `TunnelEvent`, `TunnelMetric`; extends `RemoteCommand.command_type` choices.

## Docker images

| Image / service | Rebuild for RC1? |
|-----------------|------------------|
| `django` / celery* | Yes |
| `reverse-tunnel-gateway` | Yes (first) |
| postgres, redis, guacamole, guacd, guacamole-db | No |

## Compose

```yaml
# Excerpt — see docker-compose.ra-production.yml
reverse-tunnel-gateway:
  profiles: ["guacamole"]
  build:
    context: ../ReverseTunnelGateway
    dockerfile: Dockerfile
```

**Host layout requirement:** Portal repo and `ReverseTunnelGateway` must be siblings.

## Feature flags

| Flag | RC1 ship value | Notes |
|------|----------------|-------|
| `RA_TRANSPORT` / `transport_mode` | `direct_rdp` | Do not enable `reverse_tunnel` until commissioning |
| `RA_MOCK_GUACAMOLE` | `false` in production | Existing Guacamole requirement |

## Environment variables

| Variable | Required at RC1 deploy |
|----------|-------------------------|
| `RA_TRANSPORT` | Yes → `direct_rdp` |
| `RA_TUNNEL_TOKEN_SECRET` | Yes if gateway container started |
| `RA_TUNNEL_GATEWAY_ADMIN_KEY` | Optional |
| `RA_TUNNEL_GATEWAY_ADMIN_URL` | Yes if toolkit/gateway probes used |
| `RA_TUNNEL_GATEWAY_WSS_URL` | Set for later enable; unused while direct_rdp |
| `RA_TUNNEL_ADAPTER_HOSTNAME` | Default `reverse-tunnel-gateway` |
| `TUNNEL_GATEWAY_HOST_PORT` | Optional host publish (default 7090) |

## Compatibility matrix

| Portal RC1 | Gateway | Agent | Guacamole | Notes |
|------------|---------|-------|-----------|-------|
| `direct_rdp` | Optional idle | Pre-tunnel or tunnel-capable | Required for desktop | Safe first deploy |
| `reverse_tunnel` | Required healthy | Must support JOIN/CLOSE | Required; guacd→adapter | Commissioning phase only |

## Deployment order

1. Place Gateway source sibling to Portal on host  
2. Commit/push Portal RC1; pull on host  
3. Update env (`direct_rdp` + tunnel secrets/URLs)  
4. Backup DB/config  
5. Build `django` + `reverse-tunnel-gateway`  
6. `migrate` (applies `0015`)  
7. Restart django/celery  
8. Start `reverse-tunnel-gateway`  
9. Verify health (Portal live; gateway TCP/health)  
10. Agent Windows upgrade (optional until transport flip)

## Rollback order

1. Stop `reverse-tunnel-gateway`  
2. Checkout previous Portal SHA; rebuild django; restart  
3. Restore DB backup if `0015` must be undone  
4. Confirm `transport_mode=direct_rdp` and Portal liveness  

See also: `docs/deploy/ProductionDeploymentSteps.md`, `scripts/deploy/rollback.sh`.

## Isolation verification (2026-07-31)

Unrelated working-tree changes temporarily removed (stash); RC1 Portal tests run:

- **30 passed** (reverse tunnel, commissioning toolkit/observability, analysis_platform commissioning, guacamole settings/mock)
- Portal imports of tunnel modules: OK  
- Gateway build: OK (NU1510 warning)  
- Agent build: OK  

Unrelated changes restored to working tree after verification.
