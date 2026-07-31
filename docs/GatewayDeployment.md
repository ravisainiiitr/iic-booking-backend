# Gateway Deployment

## Compose

With Guacamole profile:

```bash
export COMPOSE_FILE=docker-compose.ra-production.yml
export COMPOSE_PROFILES=guacamole
export RA_TRANSPORT=reverse_tunnel
export RA_TUNNEL_TOKEN_SECRET=<shared-secret>
export RA_TUNNEL_GATEWAY_ADMIN_URL=http://reverse-tunnel-gateway:7090/
export RA_TUNNEL_GATEWAY_WSS_URL=wss://<public-host>/tunnel
export RA_TUNNEL_ADAPTER_HOSTNAME=reverse-tunnel-gateway
./deploy.sh
python manage.py sync_remote_analysis_settings
```

Ensure edge TLS terminates WSS for agents (public WSS URL).

## Portal settings

- `transport_mode=reverse_tunnel`
- `tunnel_gateway_admin_url` internal
- `tunnel_gateway_wss_url` public
- `tunnel_adapter_hostname=reverse-tunnel-gateway`

## Agent

Upgrade RemoteAnalysisAgent to a build that includes `JOIN_TUNNEL` / `CLOSE_TUNNEL` handlers. No change to PortalBaseUrl enrollment flow.
