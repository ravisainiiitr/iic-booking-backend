# Gateway Scaling

## Phase 1 (current)

- One `reverse-tunnel-gateway` container
- Sticky sessions on `/tunnel` WSS if behind LB
- Target: tens of concurrent tunnels; lab path to 250

## Phase 2 (future)

- N Gateway replicas
- Redis (or similar) maps `tunnel_id` → gateway instance
- Allocate returns adapter hostname of the owning instance
- Agents still use a single public WSS VIP with sticky routing

## Capacity notes

- Async I/O; no thread-per-connection for RDP pump loops
- Frame buffer ~64–128 KiB
- Metrics: connected agents, active tunnels, bytes, reconnects
