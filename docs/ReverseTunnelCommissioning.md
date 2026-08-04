# Reverse Tunnel Commissioning

Validate on a lab PC with Agent + Gateway + Guacamole:

1. `RA_TRANSPORT=reverse_tunnel`, mock Guacamole off (or mock adapter path for dry-run)
2. Toolkit → Reverse Tunnel tab shows gateway health PASS
3. Launch Analysis for an eligible booking
4. Confirm `JOIN_TUNNEL` command completes on the agent
5. Confirm Gateway metrics show active tunnel + byte counters increasing
6. Complete / terminate session → `CLOSE_TUNNEL` + tunnel CLOSED
7. Negative: expired token rejected; cross-booking token rejected

Diagnostics codes: Tunnel Down, Gateway Down, Authentication Failure, Expired Token, Replay, RDP Unavailable (agent cannot connect 127.0.0.1:3389).
