# Release Notes — ReverseTunnelGateway `1.0.0-RT-RC1`

(Canonical copy also lives in the Gateway repo: `docs/RELEASE-NOTES-1.0.0-RT-RC1.md`.)

## Summary

First RC of the Reverse Tunnel Gateway bridging guacd and Windows Agents via agent-initiated WSS.

## Highlights

Allocate/close APIs · ephemeral TCP adapter · HMAC token auth · RDP byte bridge · health/metrics · Docker

## Security

Shared `RA_TUNNEL_TOKEN_SECRET`; optional admin key; do not expose admin HTTP publicly.

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
