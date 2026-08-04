# Reverse Tunnel Security

- Short-lived HMAC tokens (`RA_TUNNEL_TOKEN_SECRET` shared Portal↔Gateway)
- Claims: tunnel_id, booking_id, job_id, workstation_id, user_id, exp, nonce, session_version, permissions
- Replay: nonce+session_version registered at Gateway join
- One active tunnel per Analysis Job (Portal enforcement)
- Idle + max lifetime timeouts
- TLS for Portal HTTPS and agent WSS
- Admin API key `RA_TUNNEL_GATEWAY_ADMIN_KEY` for allocate/close/metrics — **required in Production** (Gateway refuses empty AdminKey at startup)
- Production Gateway refuses missing/insecure TokenSecret (no `change-me` / short defaults)
- Portal `tunnel_token_secret()`: when `DEBUG=False`, requires explicit `RA_TUNNEL_TOKEN_SECRET` (no silent Django `SECRET_KEY` fallback)
- Default compose keeps Gateway on internal Docker networks only; host publish needs `docker-compose.ra-gateway-host-publish.yml`
- RDP credentials remain in `WorkstationRdpSecret`; used against localhost on the PC
- Browser never receives tunnel secrets beyond normal Guacamole launch tokens
