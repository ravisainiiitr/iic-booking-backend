# Release Notes — iic-booking-backend (Portal) `1.0.0-RT-RC1`

## Summary

Portal Release Candidate for the Remote Analysis **Reverse Tunnel** transport. Additive capability behind `RA_TRANSPORT` / `transport_mode` (default **`direct_rdp`**). Includes migration `0015`, Gateway client/orchestrator, Guacamole adapter binding, compose service, toolkit probes, and live commissioning ops surfaces.

## Highlights

- `TunnelSession` / events / metrics + settings fields
- `JOIN_TUNNEL` / `CLOSE_TUNNEL` command types
- Guacamole connection provisioning via Gateway adapter when `reverse_tunnel`
- `docker-compose.ra-production.yml` → `reverse-tunnel-gateway`
- Toolkit Live Commissioning + fault injection (admin)
- Deployment / BOM / compatibility docs under `docs/release/` and `docs/deploy/`

## Security

- Short-lived HMAC tunnel tokens (`RA_TUNNEL_TOKEN_SECRET`)
- When `DEBUG=False`, Portal requires explicit `RA_TUNNEL_TOKEN_SECRET` (no Django `SECRET_KEY` fallback)
- Admin toolkit/fault endpoints remain manage-permission gated
- Evidence ZIP redacts secrets in env snapshot
- Gateway Production refuses empty AdminKey and insecure TokenSecret
- Compose: Gateway internal-only by default; optional host publish via `docker-compose.ra-gateway-host-publish.yml`

## Pins

| Component | Commit |
|-----------|--------|
| Portal | `e61fed97937b7a6df0379b3e79afc4727979fbf4` |
| Gateway | `a41ded0557b82eae01f1e741b7548806fa724dd2` |
| Agent | `f9a1bc02930c9b48dafe2f2ed72f09543a6ac275` |

## Operational changes

- New env keys: `RA_TRANSPORT`, `RA_TUNNEL_*` (see sample.env.production)
- New migration `remote_analysis.0015_reverse_tunnel_transport`
- Gateway container optional while `direct_rdp`

## Deployment notes

1. Commit/push Portal RC1 include list only  
2. Sibling Gateway + Agent `1.0.0-RT-RC1`  
3. Migrate → deploy Portal → start Gateway idle  
4. **Keep `RA_TRANSPORT=direct_rdp` until live commissioning PASS**

See `docs/deploy/ProductionDeploymentSteps.md`.

## Breaking changes

None when transport remains `direct_rdp`.

## Known limitations

- Enabling `reverse_tunnel` before Agent/Gateway ready will fail desktop path
- Public WSS edge routing for agents is an ops concern
- Unrelated desktop CSRF / reservation-window fixes are **not** in this RC commit

## Rollback

Stop gateway; `scripts/deploy/rollback.sh` / prior SHA; restore DB if undoing `0015`.
