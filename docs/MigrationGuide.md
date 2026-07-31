# Migration Guide — Reverse Tunnel Transport

## Rollout

1. Deploy Gateway compose service (Guacamole profile)
2. Set shared `RA_TUNNEL_TOKEN_SECRET` on Portal + Gateway
3. Upgrade Analysis Agents (JOIN_TUNNEL support)
4. Set `RA_TRANSPORT=reverse_tunnel` (or Admin) and sync settings
5. Point `RA_TUNNEL_GATEWAY_ADMIN_URL` / `RA_TUNNEL_GATEWAY_WSS_URL`
6. Commission one workstation end-to-end

## Rollback

```bash
export RA_TRANSPORT=direct_rdp
python manage.py sync_remote_analysis_settings
# optional: stop reverse-tunnel-gateway container
```

Existing direct RDP path is unchanged. Mock Guacamole path is unchanged.

## Compatibility

Default remains `direct_rdp`. No booking/UI changes required.
