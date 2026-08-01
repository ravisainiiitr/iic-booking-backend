# Gateway Deployment — Reverse Tunnel Gateway `1.0.0-RT-RC1`

## Production architecture (live)

| Item | Value |
|------|-------|
| Portal compose entry | **`docker-compose.production.yml`** |
| Database | **AWS RDS** (no Postgres container in this compose) |
| Cache / broker | Redis service in `docker-compose.production.yml` |
| Gateway service | `reverse-tunnel-gateway` (`--profile guacamole`) |
| Network | Project **`default`** (same as `django`) — internal only |
| Host port publish | **Off** by default; optional via `docker-compose.ra-gateway-host-publish.yml` |
| Transport for first deploy | Keep **`RA_TRANSPORT=direct_rdp`** — gateway may run **idle** |

`docker-compose.ra-production.yml` is for a **fresh-server** RA stack (local Postgres + Guacamole). Do not treat it as the live AWS production entry point.

## Prerequisites

- Sibling checkout: `../ReverseTunnelGateway` at Gateway RC1 security commit
- Env (production `.envs/.production/.django` and/or shell):  
  `RA_TUNNEL_TOKEN_SECRET`, `RA_TUNNEL_GATEWAY_ADMIN_KEY` (required; no empty / change-me defaults)  
  Portal may also set `RA_TUNNEL_GATEWAY_ADMIN_URL=http://reverse-tunnel-gateway:7090/` for later use
- **Do not** set `RA_TRANSPORT=reverse_tunnel` in this phase

## Build (do not start Portal)

```bash
cd /path/to/iic-booking-backend
export COMPOSE_FILE=docker-compose.production.yml
docker compose -f docker-compose.production.yml --profile guacamole \
  build reverse-tunnel-gateway
```

## Start Gateway only (idle)

```bash
# Secrets must already be set in the environment (compose ${VAR:?} substitution)
docker compose -f docker-compose.production.yml --profile guacamole \
  up -d reverse-tunnel-gateway
```

Do **not** restart django / celery / redis for Gateway-only commissioning.

## Verify (internal)

```bash
docker compose -f docker-compose.production.yml --profile guacamole \
  ps reverse-tunnel-gateway
docker compose -f docker-compose.production.yml --profile guacamole \
  exec reverse-tunnel-gateway bash -c 'exec 3<>/dev/tcp/127.0.0.1/7090 && echo TCP_OK'
# From Portal container (same default network):
docker compose -f docker-compose.production.yml exec django \
  python -c "import urllib.request; print(urllib.request.urlopen('http://reverse-tunnel-gateway:7090/api/v1/health', timeout=5).status)"
```

## Optional host publish

```bash
export TUNNEL_GATEWAY_HOST_PORT=7090
docker compose -f docker-compose.production.yml \
  -f docker-compose.ra-gateway-host-publish.yml \
  --profile guacamole up -d reverse-tunnel-gateway
```

## Rollback (Gateway only)

```bash
docker compose -f docker-compose.production.yml --profile guacamole \
  stop reverse-tunnel-gateway
# optional remove:
# docker compose -f docker-compose.production.yml --profile guacamole \
#   rm -sf reverse-tunnel-gateway
```

Portal remains on `direct_rdp`. No transport flip. No Portal restart required for Gateway stop.
