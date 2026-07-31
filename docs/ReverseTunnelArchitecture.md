# Reverse Tunnel Architecture

## Problem

AWS-hosted Guacamole/`guacd` cannot open inbound RDP to Analysis PCs behind IIT NAT/firewalls.

## Solution

Replace **only** the network path between `guacd` and the workstation RDP port with a reverse tunnel:

```
Browser → Portal → Guacamole → guacd → GuacamoleSocketAdapter (AWS)
                                              ↕ framed WSS
                                    Windows Agent → localhost:3389
```

The Agent initiates outbound WSS. AWS never requires inbound TCP to the PC.

## Non-goals

- Booking / scheduling / workspace sync / Guacamole REST UX unchanged
- Agent HTTPS control plane (register, heartbeat, commands, workspace) unchanged
- No VPN / Tailscale / WireGuard / SSH / public RDP

## Feature flag

`RemoteAnalysisSettings.transport_mode` / env `RA_TRANSPORT`:

| Value | Behavior |
|-------|----------|
| `direct_rdp` (default) | Existing guacd → workstation hostname:3389 |
| `reverse_tunnel` | guacd → adapter; agent joins WSS and bridges localhost:3389 |

## Components

| Component | Repo |
|-----------|------|
| Portal token + provision | `iic-booking-backend` (`tunnel.py`, `tunnel_models.py`) |
| Gateway + adapter | `ReverseTunnelGateway` |
| Agent tunnel client | `RemoteAnalysisAgent` (`Tunnel/`) |

## Sequence

1. Launch Analysis (existing)
2. `ConnectionManager.create_ephemeral` sees `reverse_tunnel`
3. Portal creates `TunnelSession`, signs token, calls Gateway allocate
4. Guacamole RDP parameters use adapter hostname/port
5. Portal enqueues `JOIN_TUNNEL` command
6. Agent opens WSS, sends JOIN+token, connects `127.0.0.1:3389`
7. Gateway pipes guacd TCP ↔ WSS frames
8. On session destroy: `CLOSE_TUNNEL` + Gateway close
