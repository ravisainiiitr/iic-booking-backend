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

## Production network (dedicated Gateway)

```
Equipment Agent
        │
        │ WSS → equip.iitr.ac.in:7090/tunnel
        ▼
ReverseTunnelGateway (0.0.0.0:7090 in container; host publish 7090:7090)
        │ admin HTTP (Docker DNS)
        ▼
Portal django  ←  RA_TUNNEL_GATEWAY_ADMIN_URL=http://reverse-tunnel-gateway:7090/
```

| Setting | Production value |
|---------|------------------|
| `RA_TUNNEL_GATEWAY_WSS_URL` | `wss://equip.iitr.ac.in:7090/tunnel` |
| `RA_TUNNEL_GATEWAY_ADMIN_URL` | `http://reverse-tunnel-gateway:7090/` |
| `RA_TUNNEL_ADAPTER_HOSTNAME` | `reverse-tunnel-gateway` |

**Not used:** Apache reverse-proxy of Gateway on 443, PathBase, or forwarded-header middleware for Gateway.  
**Ops:** Security Group inbound TCP **7090** from approved networks (infrastructure prerequisite).

## Non-goals

- Booking / scheduling / workspace sync / Guacamole REST UX unchanged
- Agent HTTPS control plane (register, heartbeat, commands, workspace) unchanged
- No VPN / Tailscale / WireGuard / SSH / public RDP

## Transport mode

`RemoteAnalysisSettings.transport_mode` / env `RA_TRANSPORT`:

| Value | Behavior |
|-------|----------|
| `reverse_tunnel` (only supported) | guacd → adapter; agent joins WSS and bridges localhost:3389 |

Direct RDP (`guacd` → workstation hostname:3389) is retired and no longer accepted.

## Components

| Component | Repo |
|-----------|------|
| Portal token + provision | `iic-booking-backend` (`tunnel.py`, `tunnel_models.py`) |
| Gateway + adapter | `ReverseTunnelGateway` |
| Agent tunnel client | `RemoteAnalysisAgent` (`Tunnel/`) |

## Sequence

1. Launch Analysis (existing)
2. `ConnectionManager.create_ephemeral` provisions a reverse tunnel
3. Portal creates `TunnelSession`, signs token, calls Gateway allocate
4. Guacamole RDP parameters use adapter hostname/port
5. Portal enqueues `JOIN_TUNNEL` command
6. Agent opens WSS, sends JOIN+token, connects `127.0.0.1:3389`
7. Gateway pipes guacd TCP ↔ WSS frames
8. On session destroy: `CLOSE_TUNNEL` + Gateway close
