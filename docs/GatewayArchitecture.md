# Gateway Architecture

ASP.NET Core Kestrel service (`ReverseTunnelGateway`).

## Responsibilities

- Validate Portal HMAC tunnel tokens
- Allocate ephemeral TCP listeners for guacd
- Accept agent WSS joins
- Multiplex RDP byte streams
- Idle / max-lifetime cleanup
- Metrics endpoint for Toolkit

## Endpoints

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/v1/health` | public |
| GET | `/api/v1/metrics` | `X-Tunnel-Admin-Key` |
| POST | `/api/v1/tunnels/allocate` | admin key + token body |
| POST | `/api/v1/tunnels/{id}/close` | admin key |
| WS | `/tunnel` | JOIN frame with signed token |

## Framing

See `Protocol/TunnelFrame.cs` — magic `0x5241`, version 1, types Hello/Join/KeepAlive/RdpData/Close/Error/JoinAck.

## Scaling (phase 1)

Single Gateway instance + sticky WSS at the load balancer.

Future: Redis affinity for multi-instance stream routing (documented in GatewayScaling.md).
