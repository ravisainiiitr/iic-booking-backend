# Reverse Tunnel SAT

## SAT-12 Reverse Tunnel

- Allocate + JOIN + byte echo (Gateway integration / mock RDP)
- Portal issues token and enqueues JOIN_TUNNEL when `reverse_tunnel`

## SAT-13 Gateway Recovery

- Agent reconnect increments reconnect_count
- Idle timeout closes tunnel
- Max lifetime closes tunnel

## SAT-14 Bandwidth

- Lab/gated: sustained transfer counters; no hard fail in CI

## SAT-15 Security

- Tampered signature rejected
- Expired token rejected
- Replay nonce rejected
- Cross-booking token cannot attach to another tunnel_id

Automated unit coverage lives in:

- `iic_booking/remote_analysis/tests/test_reverse_tunnel.py`
- `ReverseTunnelGateway.Tests`
