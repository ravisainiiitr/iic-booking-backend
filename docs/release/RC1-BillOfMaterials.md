# RC1 Bill of Materials — Platform `1.0.0-RT-RC1`

**Prepared:** 2026-07-31 · **Local commits created — not pushed**

## Portal — `iic-booking-backend`

| Item | Value |
|------|-------|
| Version | `1.0.0-RT-RC1` (`VERSION`) |
| Branch | `release/reverse-tunnel-rc1` |
| Commit | `e61fed97937b7a6df0379b3e79afc4727979fbf4` |
| Migration | `0015_reverse_tunnel_transport` |
| Compose | `docker-compose.ra-production.yml` |
| Image | `iic_booking_production_django:1.0.0-RT-RC1` |

## Gateway — `ReverseTunnelGateway`

| Item | Value |
|------|-------|
| Version | `1.0.0-RT-RC1` (`VERSION` + csproj) |
| Branch | `release/reverse-tunnel-rc1` |
| Commit | `a41ded0557b82eae01f1e741b7548806fa724dd2` |
| Image | `reverse-tunnel-gateway:1.0.0-RT-RC1` |
| Dockerfile | repo root |

## Agent — `RemoteAnalysisAgent`

| Item | Value |
|------|-------|
| Version | `1.0.0-RT-RC1` (`VERSION` + csproj) |
| Branch | `release/reverse-tunnel-rc1` |
| Commit | `f9a1bc02930c9b48dafe2f2ed72f09543a6ac275` |
| Package | Windows service / MSI (not Docker) |
| Include list | `docs/RC1-IncludeList.md` |

## Feature flags

| Flag | RC1 first-deploy value |
|------|------------------------|
| `RA_TRANSPORT` | `direct_rdp` |
| `RA_MOCK_GUACAMOLE` | `false` (production) |

## Environment variables

`RA_TRANSPORT`, `RA_TUNNEL_TOKEN_SECRET` (**required** if Gateway/Portal issue tokens; no production defaults), `RA_TUNNEL_GATEWAY_ADMIN_KEY` (**required** for Production Gateway), `RA_TUNNEL_GATEWAY_ADMIN_URL`, `RA_TUNNEL_GATEWAY_WSS_URL`, `RA_TUNNEL_ADAPTER_HOSTNAME`, `TUNNEL_GATEWAY_HOST_PORT` (**only** with `docker-compose.ra-gateway-host-publish.yml`) — see `docs/release/rc1/sample.env.production`.

## Health endpoints

Portal: `/api/v1/analysis/health/live|ready|/` · Gateway: `/api/v1/health`, `/api/v1/metrics` · Agent: loopback `:5088`

## Documentation

| Doc | Location |
|-----|----------|
| Compatibility matrix | `docs/release/CompatibilityMatrix.md` |
| Commit prep | `docs/release/RC1-CommitPreparation.md` |
| Release checklist | `docs/release/ReleaseChecklist.md` |
| Live commissioning | `docs/release/LiveCommissioningChecklist.md` |
| Deploy steps | `docs/deploy/ProductionDeploymentSteps.md` |
| Portal/Gateway/Agent release notes | `docs/release/RELEASE-NOTES-*-1.0.0-RT-RC1.md` |
| Gateway Docker/Deploy | Gateway `docs/DOCKER.md`, `docs/DEPLOYMENT.md` |
| Rollback | `scripts/deploy/rollback.sh` + deploy steps |

## Test reports (local pre-commit)

| Suite | Last result |
|-------|-------------|
| Portal RT release pytest | see pre-commit validation in Readiness / this session |
| Gateway `dotnet test` | framing tests |
| Agent `dotnet test` | full agent suite |

Record exact counts in the validation section of the session report when run.
