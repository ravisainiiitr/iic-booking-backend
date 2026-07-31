# Release Checklist — `1.0.0-RT-RC1`

Operator: _______________ Date (UTC): _______________

Mark each box only with evidence. Do not enable `reverse_tunnel` until Live Commissioning PASS.

## A. Portal commit

- [ ] Branch `release/reverse-tunnel-rc1` created from current tip
- [ ] Only RC1 include list staged (no desktop CSRF / reservation / local.py / reports)
- [ ] Manifest + BOM + CompatibilityMatrix included
- [ ] Commit message references `1.0.0-RT-RC1`
- [ ] Record SHA: `PORTAL_RC1_SHA=________________`

**Rollback:** delete local branch / reset to `ac70cfa…` if not pushed.

## B. Gateway commit

- [ ] Repository initialized as git (if not already)
- [ ] Branch `release/reverse-tunnel-rc1`
- [ ] Version / README notes `1.0.0-RT-RC1`
- [ ] `dotnet test -c Release` PASS
- [ ] Record SHA: `GATEWAY_RC1_SHA=________________`

**Rollback:** discard uncommitted tree / reset to prior tag.

## C. Agent commit

- [ ] Branch `release/reverse-tunnel-rc1`
- [ ] Tunnel handlers included (`JOIN_TUNNEL` / `CLOSE_TUNNEL`)
- [ ] Csproj Version set or documented as `1.0.0-RT-RC1`
- [ ] `dotnet test RemoteAnalysisAgent.sln -c Release` PASS
- [ ] Unrelated dirty files reviewed (include only RT-needed)
- [ ] Record SHA: `AGENT_RC1_SHA=________________`

**Rollback:** reset branch to `3c48c16…` if unused.

## D. Compatibility pins

- [ ] `docs/release/CompatibilityMatrix.md` updated with three SHAs
- [ ] Triple version string `1.0.0-RT-RC1` consistent
- [ ] Migration `0015` listed

**Rollback:** N/A (docs only).

## E. Docker build (on deploy host or CI — later)

- [ ] Sibling layout: Portal + `../ReverseTunnelGateway`
- [ ] `docker compose -f docker-compose.ra-production.yml --profile guacamole build django`
- [ ] `docker compose … build reverse-tunnel-gateway`
- [ ] Images tagged `*:1.0.0-RT-RC1` (optional but recommended)

**Rollback:** do not `up` new images; keep previous containers.

## F. Migration (deploy host — later)

- [ ] DB backup completed
- [ ] `migrate --noinput` applies `0015`
- [ ] `\dt *tunnel*` shows tables
- [ ] **Not** faked

**Rollback:** restore DB backup; checkout prior Portal image.

## G. Deployment (later)

- [ ] Env: `RA_TRANSPORT=direct_rdp` + tunnel secrets/URLs set
- [ ] Portal containers restarted on new image
- [ ] Gateway container started (idle OK)
- [ ] Agent Windows package prepared (upgrade optional until transport flip)

**Rollback:** stop gateway; prior Portal SHA + images; see ProductionDeploymentSteps.

## H. Health verification

- [ ] `GET /api/v1/analysis/health/live/` → 200
- [ ] Ready probe documented (200 or known Guacamole gap)
- [ ] Gateway health/metrics or TCP `:7090` OK
- [ ] Toolkit reverse-tunnel probe runs (auth)

**Rollback:** prior stack.

## I. Rollback drill (document)

- [ ] Written rollback for Portal / Gateway / Agent / DB known to operator
- [ ] `scripts/deploy/rollback.sh` path confirmed

## J. Commissioning

- [ ] `LiveCommissioningChecklist.md` printed / opened
- [ ] CommissioningRunId started via toolkit
- [ ] Evidence ZIP retained

## K. Go-live (only after commissioning)

- [ ] All Live Commissioning items PASS
- [ ] Explicit change control approval to set `RA_TRANSPORT=reverse_tunnel`
- [ ] First outside-IIT researcher path PASS
- [ ] Second researcher allocation PASS

**Do not check K until J is complete.**
