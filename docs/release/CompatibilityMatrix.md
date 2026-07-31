# Compatibility Matrix — Remote Analysis Platform `1.0.0-RT-RC1`

**Status:** Pre-commit (placeholders until you approve commits)  
**Updated:** 2026-07-31  
**Commissioning package version:** `1.0.0-RT-RC1` (`docs/release/LiveCommissioningChecklist.md`)

| Component | Version | Repository | Branch (planned) | Commit pin |
|-----------|---------|------------|------------------|------------|
| Platform | `1.0.0-RT-RC1` | — | — | — |
| Portal | `1.0.0-RT-RC1` | `iic-booking-backend` | `release/reverse-tunnel-rc1` | `_PORTAL_RC1_SHA_` (base today `ac70cfa61deb3554d4932be461db3ef77a5ea0c9`) |
| Gateway | `1.0.0-RT-RC1` | `ReverseTunnelGateway` | `release/reverse-tunnel-rc1` | `_GATEWAY_RC1_SHA_` (git **initialized**, **0 commits**) |
| Agent | `1.0.0-RT-RC1` | `RemoteAnalysisAgent` | `release/reverse-tunnel-rc1` | `_AGENT_RC1_SHA_` (HEAD today `3c48c16c4f2ef60a08e36dc63aad17810e371acd` + dirty) |
| Migration | `0015` | Portal | with Portal RC1 | `remote_analysis.0015_reverse_tunnel_transport` |

## Compose / Docker

| Item | Value |
|------|-------|
| Compose file | `docker-compose.ra-production.yml` |
| Compose name | `iic-ra-production` |
| Django image tag | `iic_booking_production_django:1.0.0-RT-RC1` |
| Gateway image tag | `reverse-tunnel-gateway:1.0.0-RT-RC1` |
| Gateway build context | `../ReverseTunnelGateway` |

## Transport

| Mode | Compatible for first prod deploy of RC1? |
|------|------------------------------------------|
| `RA_TRANSPORT=direct_rdp` | **YES** (required) |
| `RA_TRANSPORT=reverse_tunnel` | **NO** until Live Commissioning PASS |

## Health endpoints

| Component | Endpoint |
|-----------|----------|
| Portal | `GET /api/v1/analysis/health/live/` |
| Portal | `GET /api/v1/analysis/health/ready/` |
| Portal | `GET /api/v1/analysis/health/` |
| Portal toolkit | `/api/v1/analysis/operations/toolkit/live/` (auth) |
| Gateway | `GET /api/v1/health`, `GET /api/v1/metrics` |
| Agent | `http://127.0.0.1:5088/api/health` (on PC) |

## Cross-component

| Portal | Gateway | Agent | Migration | Transport | Compatible |
|--------|---------|-------|-----------|-----------|------------|
| `1.0.0-RT-RC1` | `1.0.0-RT-RC1` | `1.0.0-RT-RC1` | `0015` | `direct_rdp` | **YES** |
| `1.0.0-RT-RC1` | `1.0.0-RT-RC1` | `1.0.0-RT-RC1` | `0015` | `reverse_tunnel` | **YES** only after commissioning |
| any | missing | any | `0015` | `direct_rdp` | **YES** |
| `1.0.0-RT-RC1` | down | tunnel-capable | `0015` | `reverse_tunnel` | **NO** |

After each approved commit:

```bash
git -C iic-booking-backend rev-parse HEAD
git -C ReverseTunnelGateway rev-parse HEAD
git -C RemoteAnalysisAgent rev-parse HEAD
```
