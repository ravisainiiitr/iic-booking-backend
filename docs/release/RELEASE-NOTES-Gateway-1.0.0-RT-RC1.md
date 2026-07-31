# Release Notes — ReverseTunnelGateway `1.0.0-RT-RC1`

(Canonical copy also lives in the Gateway repo: `docs/RELEASE-NOTES-1.0.0-RT-RC1.md`.)

## Summary

First RC of the Reverse Tunnel Gateway bridging guacd and Windows Agents via agent-initiated WSS.

## Highlights

Allocate/close APIs · ephemeral TCP adapter · HMAC token auth · RDP byte bridge · health/metrics · Docker

## Security

- Shared `RA_TUNNEL_TOKEN_SECRET` **required** in Production (no `change-me` / empty defaults; fail-fast at startup)
- `RA_TUNNEL_GATEWAY_ADMIN_KEY` **required** in Production (empty AdminKey refused at startup; Development may leave empty for local loops)
- Default Portal compose does **not** publish `:7090` to the host; use `docker-compose.ra-gateway-host-publish.yml` only when explicitly needed

## Pins

| Component | Commit |
|-----------|--------|
| Portal | `e61fed97937b7a6df0379b3e79afc4727979fbf4` |
| Gateway | `a41ded0557b82eae01f1e741b7548806fa724dd2` |
| Agent | `f9a1bc02930c9b48dafe2f2ed72f09543a6ac275` |

## Operational changes

New compose service `reverse-tunnel-gateway`; sibling build context.

## Deployment notes

Deploy with Portal; keep Portal on `direct_rdp` until commissioning PASS.

## Breaking changes

None (additive).

## Known limitations

Single-instance RC1; edge TLS for public WSS is external.

## Rollback

`docker compose … stop reverse-tunnel-gateway`
