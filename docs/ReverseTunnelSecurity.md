# Reverse Tunnel Security

- Short-lived HMAC tokens (`RA_TUNNEL_TOKEN_SECRET` shared Portal↔Gateway)
- Claims: tunnel_id, booking_id, job_id, workstation_id, user_id, exp, nonce, session_version, permissions
- Replay: nonce+session_version registered at Gateway join
- One active tunnel per Analysis Job (Portal enforcement)
- Idle + max lifetime timeouts
- TLS for Portal HTTPS and agent WSS
- Admin API key `RA_TUNNEL_GATEWAY_ADMIN_KEY` for allocate/close/metrics
- RDP credentials remain in `WorkstationRdpSecret`; used against localhost on the PC
- Browser never receives tunnel secrets beyond normal Guacamole launch tokens
