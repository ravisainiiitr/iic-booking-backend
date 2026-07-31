# Production Deployment Steps — Reverse Tunnel Release (Manual)

**Audience:** Operator with SSH to the production EC2 host  
**Mode:** Manual deploy only — this document is prepared offline.  
**This guide does not deploy anything.** Copy-paste commands on the server yourself.

**Compose file used by RA scripts:** `docker-compose.ra-production.yml`  
(Do **not** use `docker-compose.production.yml` for the Reverse Tunnel Gateway service — that file does not define it.)

**Transport policy for this deploy:** leave `RA_TRANSPORT=direct_rdp` (or unset → settings default).  
**Do not** set `RA_TRANSPORT=reverse_tunnel` in this phase. Gateway may be deployed idle.

---

## Release Readiness (inspected locally — 2026-07-31)

### BLOCKER — not deployable from git as-is

| Check | Status | Detail |
|-------|--------|--------|
| Committed on `origin/master` | ✗ incomplete | Tip `ac70cfa` has **no** reverse-tunnel Portal code, **no** migration `0015`, **no** gateway compose service |
| Staged | ✓ empty | Nothing staged |
| Unstaged (Portal) | ✗ pending | See list below — includes tunnel wiring + compose |
| Untracked (Portal) | ✗ pending | `tunnel.py`, `tunnel_models.py`, `0015_…`, live commissioning, docs, tests |
| ReverseTunnelGateway in git | ✗ missing | Sibling folder `../ReverseTunnelGateway` — **not a git repository** |
| Agent tunnel handlers | ⚠ separate | Present in `RemoteAnalysisAgent` local tree — ship Agent MSI/binaries separately |
| Pending migration (uncommitted) | ✗ | `iic_booking/remote_analysis/migrations/0015_reverse_tunnel_transport.py` |
| Pending configuration | ✗ | Must add tunnel env keys to production `.envs/.production/.django` **without** enabling `reverse_tunnel` |

**You cannot `git pull` a working Reverse Tunnel release until the Portal changes are committed and pushed, and the Gateway source is available beside the Portal on the host (`../ReverseTunnelGateway`).**

### Local verification (prep machine — not production)

| Check | Result |
|-------|--------|
| Python compile (`tunnel*`, live commissioning, fault injection) | ✓ exit 0 |
| Pytest (tunnel + commissioning suite) | ✓ **18 passed** |
| `dotnet build` ReverseTunnelGateway Release | ✓ 0 errors (NU1510 warning only) |
| `dotnet build` RemoteAnalysisAgent Release | ✓ 0 errors |
| Docker image build | not run (per instruction: do not run docker for this prep) |
| SSH / production mutate | not done |

### Exact git commit hash (current committed tip)

```
ac70cfa61deb3554d4932be461db3ef77a5ea0c9
```

Branch: local `main` → `origin/master` at the same tip.  
**Release SHA for Reverse Tunnel:** *TBD — create a commit that includes the files below, then replace this section with that SHA before deploying.*

### Unstaged modified files (must be in the release commit)

```
config/settings/base.py
config/settings/local.py
docker-compose.ra-production.yml
docs/RemoteAnalysisLiveCommissioning.md
docs/release/rc1/sample.env.production
iic_booking/equipment/remote_analysis_integration/desktop_html.py
iic_booking/equipment/remote_analysis_integration/views.py
iic_booking/remote_analysis/admin.py
iic_booking/remote_analysis/configuration_catalog.py
iic_booking/remote_analysis/constants.py
iic_booking/remote_analysis/guacamole/connection.py
iic_booking/remote_analysis/guacamole/settings_env.py
iic_booking/remote_analysis/models.py
iic_booking/remote_analysis/operations/commissioning_observability.py
iic_booking/remote_analysis/operations/toolkit.py
iic_booking/remote_analysis/operations/toolkit_html.py
iic_booking/remote_analysis/operations/toolkit_views.py
iic_booking/remote_analysis/operations/views.py
iic_booking/remote_analysis/services/reservation.py
iic_booking/remote_analysis/session_models.py
iic_booking/remote_analysis/tests/test_commissioning_observability.py
iic_booking/remote_analysis/tests/test_commissioning_toolkit.py
iic_booking/remote_analysis/urls.py
```

### Untracked files required for Reverse Tunnel / commissioning (must be in the release commit)

```
iic_booking/remote_analysis/migrations/0015_reverse_tunnel_transport.py
iic_booking/remote_analysis/tunnel.py
iic_booking/remote_analysis/tunnel_models.py
iic_booking/remote_analysis/operations/fault_injection.py
iic_booking/remote_analysis/operations/live_commissioning.py
iic_booking/remote_analysis/operations/live_commissioning_html.py
iic_booking/remote_analysis/tests/test_booking_analysis_window.py
iic_booking/remote_analysis/tests/test_reverse_tunnel.py
tests/analysis_platform/test_commissioning.py
docs/GatewayArchitecture.md
docs/GatewayDeployment.md
docs/GatewayScaling.md
docs/MigrationGuide.md
docs/RemoteAnalysisPhase4LiveCommissioning.md
docs/ReverseTunnelArchitecture.md
docs/ReverseTunnelCommissioning.md
docs/ReverseTunnelSAT.md
docs/ReverseTunnelSecurity.md
docs/ReverseTunnelTroubleshooting.md
docs/release/phase4/*
docs/deploy/ProductionDeploymentSteps.md   # this file (after you add it)
```

### Optional / do not ship to production tree

```
reports/analysis_platform/*    # local harness output — exclude from release
config/settings/local.py       # prefer not to rely on local-only settings in prod commit review
```

### Migrations

| Migration | Purpose | On origin? |
|-----------|---------|------------|
| `remote_analysis.0014_analysis_workflows` | Workflows | ✓ committed |
| **`remote_analysis.0015_reverse_tunnel_transport`** | `transport_mode`, tunnel settings fields, `TunnelSession` / `TunnelEvent` / `TunnelMetric` | ✗ **untracked** |

Depends on: `remote_analysis.0014`, `equipment.0181`.

### Docker images that must be rebuilt (after release is on the host)

| Image / service | Rebuild? | Why |
|-----------------|----------|-----|
| `iic_booking_production_django` (`django`, celery*) | **Yes** | Portal tunnel + migration code |
| `reverse-tunnel-gateway` | **Yes** (first deploy) | New service; build context `../ReverseTunnelGateway` |
| `guacamole` / `guacd` / `guacamole-db` | No (unless image tag change) | Unchanged images |
| `postgres` / `redis` | No | Unchanged |

### Environment variables (add to production env; do **not** enable reverse tunnel yet)

Source template: `docs/release/rc1/sample.env.production`

```bash
# Keep transport on direct_rdp for this deploy
RA_TRANSPORT=direct_rdp

# Shared HMAC Portal ↔ Gateway (required even if transport stays direct_rdp, so gateway can start)
RA_TUNNEL_TOKEN_SECRET=<set-strong-secret>
RA_TUNNEL_GATEWAY_ADMIN_KEY=<set-strong-admin-key-required>

# Internal compose DNS (Portal → Gateway admin HTTP)
RA_TUNNEL_GATEWAY_ADMIN_URL=http://reverse-tunnel-gateway:7090/

# Public WSS for agents (configure edge TLS later; unused while direct_rdp)
RA_TUNNEL_GATEWAY_WSS_URL=wss://equip.iitr.ac.in/tunnel

RA_TUNNEL_ADAPTER_HOSTNAME=reverse-tunnel-gateway

# Guacamole: production should use real Guacamole (ready probe currently fails if mock + DEBUG=False)
RA_MOCK_GUACAMOLE=false
# …existing RA_GUACAMOLE_* remain as already configured…
```

Host publish is **not** the default. Gateway stays on internal Docker networks only.
Optional host publish requires an explicit override file and port:

```bash
export TUNNEL_GATEWAY_HOST_PORT=7090
docker compose -f docker-compose.ra-production.yml \
  -f docker-compose.ra-gateway-host-publish.yml \
  --profile guacamole up -d reverse-tunnel-gateway
```

### Deployment scripts (present in repo)

| Script | Role |
|--------|------|
| `./deploy.sh` → `scripts/deploy/deploy.sh` | Backup config, pull, build, migrate, up, verify |
| `scripts/deploy/rollback.sh` | Checkout previous ref, rebuild, migrate, verify |
| `scripts/deploy/verify-production.sh` | Live/ready/health + optional toolkit |
| `scripts/deploy/validate-startup.sh` | Wrapper around `validate_deployment_startup` |
| `scripts/deploy/backup.sh` | DB/config backup |
| `scripts/deploy/lib.sh` | `COMPOSE_FILE=docker-compose.ra-production.yml`, profile `guacamole` |

### Health endpoints (use after deploy)

| URL | Expect (this phase) |
|-----|---------------------|
| `GET /api/v1/analysis/health/live/` | **200** `status=ok` |
| `GET /api/v1/analysis/health/ready/` | **200** only if Guacamole not mock; today prod may still be **503** if mock |
| `GET /api/v1/analysis/health/` | **200** when ready |
| Gateway (from host/docker network) | `GET http://127.0.0.1:7090/health` (confirm path on gateway; TCP `:7090` open at minimum) |
| Toolkit (auth) | `/api/v1/analysis/operations/toolkit/live/?view=html` |

---

## Deployment Checklist (operator)

- [ ] Release commit created and pushed (Portal + compose gateway service)
- [ ] Note **NEW_RELEASE_SHA** and **PREVIOUS_SHA** (`ac70cfa…` or whatever is on prod today)
- [ ] `ReverseTunnelGateway` source placed as sibling of Portal on server: `../ReverseTunnelGateway`
- [ ] Production env updated with tunnel vars; **`RA_TRANSPORT=direct_rdp`**
- [ ] Config/DB backup taken
- [ ] Portal image rebuilt; django/celery restarted
- [ ] Migration `0015` applied (no `--fake`)
- [ ] Gateway container built and healthy
- [ ] Guacamole / guacd still healthy
- [ ] Health curls recorded
- [ ] Booking portal smoke (login page / API) — no analysis booking test required this phase
- [ ] Agent Windows package prepared separately (do not enable JOIN_TUNNEL traffic until transport flip later)

---

## Step 0 — Capture previous revision (on server)

**Purpose:** Record rollback target before changing anything.

**Command:**

```bash
cd ~/iic-booking-backend   # or /opt/iic-booking-backend — use your real path
git rev-parse HEAD
git status -sb
docker compose -f docker-compose.ra-production.yml --profile guacamole ps
```

**Expected Output:** Current SHA printed; containers listed.

**Success Criteria:** You saved `PREVIOUS_SHA=…` offline.

**Rollback:** N/A (read-only).

---

## Step 1 — Git update (Portal)

**Purpose:** Check out the release that contains Reverse Tunnel Portal code.

**Command:**

```bash
cd ~/iic-booking-backend
git fetch origin
git checkout master          # or main, matching remote default
git pull --ff-only origin master
git rev-parse HEAD
# Expect: NEW_RELEASE_SHA (must include migration 0015 + tunnel modules)
ls iic_booking/remote_analysis/migrations/0015_reverse_tunnel_transport.py
ls iic_booking/remote_analysis/tunnel.py
grep -n reverse-tunnel-gateway docker-compose.ra-production.yml
```

**Expected Output:** Fast-forward or already up to date; `0015` and `tunnel.py` exist; compose contains `reverse-tunnel-gateway`.

**Success Criteria:** `git rev-parse HEAD` equals the release SHA you approved; blocker files present.

**Rollback:**

```bash
git checkout PREVIOUS_SHA
```

---

## Step 2 — Place Gateway source (sibling directory)

**Purpose:** Satisfy compose build context `../ReverseTunnelGateway`.

**Command:**

```bash
# From parent of Portal repo, e.g. /home/ubuntu
cd ~
ls ReverseTunnelGateway/Dockerfile
# If missing: copy/rsync/clone Gateway tree so that:
#   ~/iic-booking-backend/docker-compose.ra-production.yml
#   ~/ReverseTunnelGateway/Dockerfile
test -f ~/ReverseTunnelGateway/Dockerfile && echo GATEWAY_OK
```

**Expected Output:** `GATEWAY_OK`

**Success Criteria:** Dockerfile readable from compose relative path.

**Rollback:** Remove or rename the Gateway tree only if you also remove the compose service (normally leave files; stop container instead).

---

## Step 3 — Environment (do not enable reverse_tunnel)

**Purpose:** Load secrets so Portal and Gateway share HMAC; keep transport direct.

**Command:**

```bash
# Edit production env (path used by compose)
nano ~/iic-booking-backend/.envs/.production/.django
# Ensure:
#   RA_TRANSPORT=direct_rdp
#   RA_TUNNEL_TOKEN_SECRET=...
#   RA_TUNNEL_GATEWAY_ADMIN_URL=http://reverse-tunnel-gateway:7090/
#   RA_TUNNEL_GATEWAY_WSS_URL=wss://equip.iitr.ac.in/tunnel
#   RA_TUNNEL_ADAPTER_HOSTNAME=reverse-tunnel-gateway
#   RA_MOCK_GUACAMOLE=false   # if Guacamole stack is real

grep -E '^RA_TRANSPORT=|^RA_TUNNEL_|^RA_MOCK_GUACAMOLE=' .envs/.production/.django
```

**Expected Output:** `RA_TRANSPORT=direct_rdp` and tunnel URLs present; secrets not printed in tickets.

**Success Criteria:** Transport still `direct_rdp`.

**Rollback:** Restore env from `backups/deploy/config-*` copy.

---

## Step 4 — Backup before migrate

**Purpose:** Reversible DB/config state.

**Command:**

```bash
cd ~/iic-booking-backend
./scripts/deploy/backup.sh --label "pre-tunnel-$(date -u +%Y%m%dT%H%M%SZ)"
# or at minimum:
cp -a .envs/.production/.django "backups/deploy/config-manual-$(date -u +%Y%m%dT%H%M%SZ)/"
```

**Expected Output:** Backup directory created without error.

**Success Criteria:** You can restore `.django` env and DB dump if needed.

**Rollback:** Use that backup in failure handling.

---

## Step 5 — Build images

**Purpose:** Rebuild Portal Django image and Gateway image.

**Command:**

```bash
cd ~/iic-booking-backend
export COMPOSE_FILE=docker-compose.ra-production.yml
export COMPOSE_PROFILES=guacamole

docker compose -f docker-compose.ra-production.yml --profile guacamole build django
docker compose -f docker-compose.ra-production.yml --profile guacamole build reverse-tunnel-gateway
```

**Expected Output:** Build succeeds; no missing context for `../ReverseTunnelGateway`.

**Success Criteria:** Both images built; no error exit.

**Rollback:** Do not `up` yet; previous containers still running.

---

## Step 6 — Database migrations

**Purpose:** Apply only pending migrations (including `0015`). Do not fake.

**Command:**

```bash
cd ~/iic-booking-backend
docker compose -f docker-compose.ra-production.yml --profile guacamole run --rm --no-deps django \
  python manage.py showmigrations remote_analysis | tail -20

docker compose -f docker-compose.ra-production.yml --profile guacamole run --rm --no-deps django \
  python manage.py migrate --noinput

docker compose -f docker-compose.ra-production.yml --profile guacamole run --rm --no-deps django \
  python manage.py showmigrations remote_analysis | grep 0015
```

**Expected Output:** `0015_reverse_tunnel_transport` marked `[X]` after migrate.

**Success Criteria:** Migrate exit 0; no faked migrations; tables for tunnel models exist:

```bash
docker compose -f docker-compose.ra-production.yml exec postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt *tunnel*"
```

**Rollback:** Restore DB dump from Step 4, then checkout `PREVIOUS_SHA` and rebuild Portal. Schema forward-only without restore is unsafe if you must fully undo `0015`.

---

## Step 7 — Restart Portal stack (affected services)

**Purpose:** Run new Portal code; keep transport direct_rdp.

**Command:**

```bash
cd ~/iic-booking-backend
docker compose -f docker-compose.ra-production.yml --profile guacamole up -d django celeryworker celerybeat
# If your service names differ, adjust from: docker compose … ps
docker compose -f docker-compose.ra-production.yml --profile guacamole ps
docker compose -f docker-compose.ra-production.yml logs django --tail=100
```

**Expected Output:** Containers `Up` / healthy; no migration/startup traceback in logs.

**Success Criteria:** No repeated exceptions in django logs for 1–2 minutes.

**Rollback:**

```bash
git checkout PREVIOUS_SHA
docker compose -f docker-compose.ra-production.yml --profile guacamole build django
docker compose -f docker-compose.ra-production.yml --profile guacamole up -d django celeryworker celerybeat
# Restore DB if 0015 must be undone
```

---

## Step 8 — Start Reverse Tunnel Gateway

**Purpose:** Deploy gateway container idle (Portal still `direct_rdp`).

**Command:**

```bash
cd ~/iic-booking-backend
docker compose -f docker-compose.ra-production.yml --profile guacamole up -d reverse-tunnel-gateway
docker compose -f docker-compose.ra-production.yml --profile guacamole ps reverse-tunnel-gateway
docker compose -f docker-compose.ra-production.yml logs reverse-tunnel-gateway --tail=100
```

**Expected Output:** Container running; healthcheck passing; no crash loop.

**Success Criteria:** Port `7090` listening inside container/network.

```bash
docker compose -f docker-compose.ra-production.yml --profile guacamole exec reverse-tunnel-gateway \
  bash -c 'exec 3<>/dev/tcp/127.0.0.1/7090 && echo TCP_OK'
# Or from host if published:
curl -sS -o /tmp/gw_health.txt -w "%{http_code}\n" --max-time 5 http://127.0.0.1:7090/health || true
curl -sS -o /tmp/gw_metrics.txt -w "%{http_code}\n" --max-time 5 http://127.0.0.1:7090/metrics || true
cat /tmp/gw_health.txt /tmp/gw_metrics.txt
```

**Rollback:**

```bash
docker compose -f docker-compose.ra-production.yml --profile guacamole stop reverse-tunnel-gateway
# or: docker compose … rm -sf reverse-tunnel-gateway
```

---

## Step 9 — Verify Guacamole (unchanged behaviour)

**Purpose:** Confirm Guacamole stack still healthy after compose profile restart.

**Command:**

```bash
docker compose -f docker-compose.ra-production.yml --profile guacamole ps guacd guacamole guacamole-db
docker compose -f docker-compose.ra-production.yml logs guacd --tail=50
docker compose -f docker-compose.ra-production.yml logs guacamole --tail=50
```

**Expected Output:** All three Up/healthy; no new startup failures.

**Success Criteria:** Existing Guacamole access path still works for ops (no live booking test required).

**Rollback:** Restart guac services only; do not change Portal transport.

---

## Step 10 — Sync settings from env (optional, safe)

**Purpose:** Load env into `RemoteAnalysisSettings` without flipping transport if env says `direct_rdp`.

**Command:**

```bash
docker compose -f docker-compose.ra-production.yml --profile guacamole run --rm --no-deps django \
  python manage.py sync_remote_analysis_settings
```

**Expected Output:** Command completes; settings show `transport_mode=direct_rdp`.

**Success Criteria:** Confirm in Django admin or shell that transport is still `direct_rdp`.

**Rollback:** Re-set `RA_TRANSPORT=direct_rdp` and re-run sync; or restore settings from backup notes.

---

## Step 11 — Health verification (Portal)

**Purpose:** Record HTTP code, latency, payload.

**Command:**

```bash
PORTAL_BASE_URL=https://equip.iitr.ac.in

curl -sS -w "\nHTTP %{http_code} time=%{time_total}\n" \
  "$PORTAL_BASE_URL/api/v1/analysis/health/live/"

curl -sS -w "\nHTTP %{http_code} time=%{time_total}\n" \
  "$PORTAL_BASE_URL/api/v1/analysis/health/ready/"

curl -sS -w "\nHTTP %{http_code} time=%{time_total}\n" \
  "$PORTAL_BASE_URL/api/v1/analysis/health/"

# Authenticated toolkit (optional)
# ADMIN_TOKEN=… curl -sS -H "Authorization: Token $ADMIN_TOKEN" \
#   "$PORTAL_BASE_URL/api/v1/analysis/operations/toolkit/health-report/"

PORTAL_BASE_URL="$PORTAL_BASE_URL" ./scripts/deploy/verify-production.sh || true
```

**Expected Output:** Liveness **200**. Readiness **200** only if Guacamole configured non-mock; otherwise document **503** with known reason.

**Success Criteria:** No new Portal startup regressions; booking site root still **200**.

**Rollback:** See full rollback section below.

---

## Step 12 — Feature flag report (do not change)

**Purpose:** Snapshot flags after deploy.

**Command:**

```bash
grep -E '^RA_TRANSPORT=|^RA_TUNNEL_|^RA_MOCK_GUACAMOLE=' .envs/.production/.django
docker compose -f docker-compose.ra-production.yml run --rm --no-deps django \
  python manage.py shell -c "from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings as S; s=S.get_solo(); print('transport_mode=', s.transport_mode); print('mock_guacamole=', s.mock_guacamole); print('admin_url=', s.tunnel_gateway_admin_url); print('wss_url=', s.tunnel_gateway_wss_url)"
```

**Expected Output:**

```
RA_TRANSPORT=direct_rdp
transport_mode= direct_rdp
```

**Success Criteria:** Reverse tunnel **not** enabled for users.

**Rollback:** N/A (read-only check).

---

## Step 13 — Agent package (Windows PC — separate)

**Purpose:** Prepare Agent with `JOIN_TUNNEL` / `CLOSE_TUNNEL` for a later commissioning phase.  
**Do not** require agent upgrade to complete this Portal/Gateway deploy if you are not enabling reverse_tunnel yet.

**Command (on build PC):**

```powershell
cd D:\IIC_NEW\RemoteAnalysisAgent
dotnet build src\RemoteAnalysisAgent\RemoteAnalysisAgent.csproj -c Release
# Package per your MSI/scripts under scripts\ — then copy to Analysis PC
```

**On Analysis PC (later):** stop service → replace binaries → start → heartbeat in Toolkit.

**Expected Output:** Build succeeded; service online after upgrade.

**Success Criteria:** Heartbeat age ≤ 90s. Tunnel unused until transport flip.

**Rollback:** Reinstall previous Agent build.

---

## One-shot alternative (after release is pushed)

**Purpose:** Use existing deploy script once blockers are cleared.

**Command:**

```bash
cd ~/iic-booking-backend
export COMPOSE_FILE=docker-compose.ra-production.yml
export COMPOSE_PROFILES=guacamole
export PORTAL_BASE_URL=https://equip.iitr.ac.in
# Ensure ReverseTunnelGateway sibling exists
./scripts/deploy/deploy.sh
```

**Expected Output:** Script completes migrate + up + verify.

**Success Criteria:** Same as Steps 5–11.

**Rollback:** `./scripts/deploy/rollback.sh` or `ROLLBACK_REF=PREVIOUS_SHA ./scripts/deploy/rollback.sh`

---

## Full rollback

**Purpose:** Restore previous Portal code/images; stop gateway if needed.

**Command:**

```bash
cd ~/iic-booking-backend
export COMPOSE_FILE=docker-compose.ra-production.yml
export COMPOSE_PROFILES=guacamole
export SKIP_GIT_PULL=1

# Stop gateway (safe — not used while direct_rdp)
docker compose -f docker-compose.ra-production.yml --profile guacamole stop reverse-tunnel-gateway

# Scripted rollback to previous ref
ROLLBACK_REF=PREVIOUS_SHA ./scripts/deploy/rollback.sh

# If schema 0015 must be removed: restore DB dump from pre-tunnel backup, then:
# gunzip -c backups/deploy/<label>/db/portal.sql.gz | \
#   docker compose -f docker-compose.ra-production.yml exec -T postgres \
#   sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

**Expected Output:** Previous SHA running; RA liveness 200; booking root 200.

**Success Criteria:** Production behaviour matches pre-deploy baseline.

---

## Success criteria (this phase)

| Criterion | Required |
|-----------|----------|
| Portal release with tunnel code on server | ✓ |
| Migration `0015` applied | ✓ |
| Gateway container healthy | ✓ |
| `RA_TRANSPORT` / `transport_mode` still `direct_rdp` | ✓ |
| Guacamole stack not broken | ✓ |
| No live booking / reverse_tunnel user enable | ✓ (out of scope) |
| Agent upgrade | Optional until commissioning |

---

## What prevents successful deployment right now

1. **Uncommitted Portal Reverse Tunnel work** — `git pull` on production will **not** get tunnel code until you commit/push a release.  
2. **Gateway not in Portal git** — host must have `../ReverseTunnelGateway` with Dockerfile.  
3. **Compose build context** — without sibling Gateway, `docker compose build reverse-tunnel-gateway` fails.  
4. **Production Guacamole readiness** — historically `mock_forbidden_when_debug_false`; fix Guacamole config separately if you need ready=200.  
5. **Do not enable `reverse_tunnel`** in this deploy — gateway idle only.

---

## Related docs

- [Production-Deployment-Guide.md](Production-Deployment-Guide.md)  
- [Operations-Runbook-IITR.md](Operations-Runbook-IITR.md)  
- [MONITORING.md](MONITORING.md)  
- [AGENT_INSTALL.md](AGENT_INSTALL.md)  
- [../GatewayDeployment.md](../GatewayDeployment.md)  
- [../MigrationGuide.md](../MigrationGuide.md)  
- [../RemoteAnalysisPhase4LiveCommissioning.md](../RemoteAnalysisPhase4LiveCommissioning.md)  
