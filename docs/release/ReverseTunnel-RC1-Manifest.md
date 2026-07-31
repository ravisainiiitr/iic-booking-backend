# Reverse Tunnel — Release Candidate Manifest (RC1)

**Release version:** `ReverseTunnel-RC1`  
**Status:** **NOT READY TO COMMIT** until exclusions are applied and Gateway/Agent packaging is decided  
**Base commit (current origin tip):** `ac70cfa61deb3554d4932be461db3ef77a5ea0c9`  
**RC commit SHA:** `_PLACEHOLDER — replace after commit_`  
**Prepared:** 2026-07-31 (local audit; no push/deploy)

**Default feature flag for first production deploy of this RC:** `RA_TRANSPORT=direct_rdp`  
(Gateway may ship idle. Do not enable `reverse_tunnel` until commissioning.)

---

## 1. Audit — file classification

### 1. Reverse Tunnel Core

| Path | State | Notes |
|------|-------|-------|
| `iic_booking/remote_analysis/tunnel.py` | untracked | Token, Gateway client, orchestrator |
| `iic_booking/remote_analysis/tunnel_models.py` | untracked | TunnelSession / Event / Metric |
| `iic_booking/remote_analysis/constants.py` | modified | TransportMode, TunnelSessionStatus, JOIN/CLOSE commands |
| `iic_booking/remote_analysis/session_models.py` | modified | transport + tunnel settings fields |
| `iic_booking/remote_analysis/guacamole/connection.py` | modified | Provision tunnel; adapter hostname/port for Guacamole |
| `iic_booking/remote_analysis/guacamole/settings_env.py` | modified | `RA_TRANSPORT`, tunnel URL env overlays |
| `iic_booking/remote_analysis/configuration_catalog.py` | modified | Catalog keys for transport/tunnel |
| `iic_booking/remote_analysis/models.py` | modified | Import tunnel models for app registry |
| `iic_booking/remote_analysis/admin.py` | modified | Tunnel admin + settings fields |
| *(sibling)* `../ReverseTunnelGateway/**` | **outside Portal git** | ASP.NET Gateway |
| *(sibling)* `../RemoteAnalysisAgent/…/Tunnel/**` | **outside Portal git** | JOIN/CLOSE handlers |

### 2. Deployment

| Path | State | Notes |
|------|-------|-------|
| `docker-compose.ra-production.yml` | modified | `reverse-tunnel-gateway` service |
| `docs/release/rc1/sample.env.production` | modified | Tunnel env sample |
| `docs/deploy/ProductionDeploymentSteps.md` | untracked | Manual deploy guide |
| `docs/deploy/README.md` | modified | Link to deploy steps |
| `docs/GatewayDeployment.md` | untracked | Gateway deploy notes |
| `docs/MigrationGuide.md` | untracked | Transport migration notes |
| `scripts/deploy/*` | unchanged on origin | Already present; no local edits this RC |

### 3. Database

| Path | State | Notes |
|------|-------|-------|
| `iic_booking/remote_analysis/migrations/0015_reverse_tunnel_transport.py` | untracked | Settings fields + tunnel tables + command choices |

### 4. Documentation

| Path | State |
|------|-------|
| `docs/ReverseTunnelArchitecture.md` | untracked |
| `docs/ReverseTunnelCommissioning.md` | untracked |
| `docs/ReverseTunnelSAT.md` | untracked |
| `docs/ReverseTunnelSecurity.md` | untracked |
| `docs/ReverseTunnelTroubleshooting.md` | untracked |
| `docs/GatewayArchitecture.md` | untracked |
| `docs/GatewayScaling.md` | untracked |
| `docs/RemoteAnalysisLiveCommissioning.md` | modified (Phase 4 pointer) |
| `docs/RemoteAnalysisPhase4LiveCommissioning.md` | untracked |
| `docs/release/phase4/*` | untracked |
| `docs/release/ReverseTunnel-RC1-Manifest.md` | this file |

### 5. Tests

| Path | State |
|------|-------|
| `iic_booking/remote_analysis/tests/test_reverse_tunnel.py` | untracked |
| `iic_booking/remote_analysis/tests/test_commissioning_toolkit.py` | modified (live/fault) |
| `iic_booking/remote_analysis/tests/test_commissioning_observability.py` | modified (evidence ZIP members) |
| `tests/analysis_platform/test_commissioning.py` | untracked |
| `iic_booking/remote_analysis/tests/test_booking_analysis_window.py` | untracked — **ties to reservation fix (see §3)** |

### 6. Tooling / Commissioning

| Path | State |
|------|-------|
| `iic_booking/remote_analysis/operations/live_commissioning.py` | untracked |
| `iic_booking/remote_analysis/operations/live_commissioning_html.py` | untracked |
| `iic_booking/remote_analysis/operations/fault_injection.py` | untracked |
| `iic_booking/remote_analysis/operations/commissioning_observability.py` | modified (timeline steps + evidence) |
| `iic_booking/remote_analysis/operations/toolkit.py` | modified (`probe_reverse_tunnel`) |
| `iic_booking/remote_analysis/operations/toolkit_html.py` | modified |
| `iic_booking/remote_analysis/operations/toolkit_views.py` | modified (live/faults APIs) |
| `iic_booking/remote_analysis/operations/views.py` | modified (re-exports) |
| `iic_booking/remote_analysis/urls.py` | modified |

### 7. Unrelated / out-of-scope for ReverseTunnel-RC1

| Path | State | Recommendation |
|------|-------|----------------|
| `config/settings/base.py` | modified | Auth class order (token before session) for desktop CSRF — **remain uncommitted** or **separate commit** (`fix(desktop): prioritize token auth`) |
| `config/settings/local.py` | modified | Local `CSRF_TRUSTED_ORIGINS` — **remain uncommitted** (dev-only) |
| `iic_booking/equipment/.../desktop_html.py` | modified | Embed CSRF in launcher HTML — **separate commit** |
| `iic_booking/equipment/.../views.py` | modified | Pass `csrf_token` in desktop payload — **same separate commit** |
| `iic_booking/remote_analysis/services/reservation.py` | modified | Completed-booking analysis window — **separate commit** (allocation fix, not tunnel) |
| `iic_booking/remote_analysis/tests/test_booking_analysis_window.py` | untracked | Goes with reservation commit |
| `reports/analysis_platform/*` | untracked | **Remain uncommitted** (local harness artifacts) |

---

## 2. Release content verification

| Required item | Present? |
|---------------|----------|
| Migration `0015` | ✓ (untracked) |
| Tunnel models | ✓ |
| Gateway client / orchestrator (`tunnel.py`) | ✓ |
| Transport feature flag (`transport_mode` / `RA_TRANSPORT`) | ✓ |
| Docker compose gateway service | ✓ |
| Deployment guide | ✓ `docs/deploy/ProductionDeploymentSteps.md` |
| Health endpoints (Portal RA live/ready) | ✓ existing; toolkit probe for gateway ✓ |
| Diagnostics / toolkit reverse-tunnel panel | ✓ |
| Commissioning live + fault injection | ✓ |
| Portal tests for tunnel | ✓ |
| Gateway source in Portal repo | ✗ **missing** — sibling only |
| Agent tunnel binaries in Portal repo | ✗ **missing** — separate repo |
| Gateway git tag / version pin in compose | ✗ no image digest; build context only |
| WSS edge/TLS routing docs for `equip.iitr.ac.in/tunnel` | ⚠ partial (env sample + GatewayDeployment) |
| Explicit reverse of `0015` documented | ✓ Django auto-reverse for AddField/CreateModel; no RunPython |

**Missing for a complete multi-component RC:** versioned Gateway package + Agent MSI/build artifact references in the Portal release notes.

---

## 3. Build verification (local)

| Component | Result |
|-----------|--------|
| Portal (`compileall` tunnel modules) | ✓ |
| Gateway `dotnet build` Release | ✓ (NU1510 warning only — non-blocking) |
| Agent `dotnet build` Release | ✓ |

---

## 4. Test verification (local)

| Suite | Result |
|-------|--------|
| Portal Reverse Tunnel + commissioning + Guacamole settings/mock | ✓ **32 passed** |
| Gateway `dotnet test` | ✓ **2 passed** |
| Agent `dotnet test` (`RemoteAnalysisAgent.sln`) | ✓ **20 passed** |
| Full platform regression (entire repo) | not run (out of RC scope) |

Last Portal command set: `test_reverse_tunnel`, `test_booking_analysis_window`, commissioning toolkit/observability, analysis_platform commissioning, guacamole mock + settings_env — **exit 0**.

---

## 5. Migration verification

| Check | Result |
|-------|--------|
| Number uniqueness | ✓ only one `0015_*` |
| Depends on `0014_analysis_workflows` | ✓ |
| Depends on `equipment.0181` | ✓ |
| Local `showmigrations` shows `[X] 0015` | ✓ (applied on local test DB) |
| Custom `RunPython` | none — standard schema ops |
| Reversible | ✓ Django can reverse AddField/CreateModel/AlterField/AddIndex (no irreversible ops) |

---

## 6. Deployment verification

| Artifact | Status |
|----------|--------|
| `compose/production/django/Dockerfile` | ✓ unchanged; rebuild django image |
| Gateway `Dockerfile` | ✓ in sibling repo |
| `docker-compose.ra-production.yml` | ✓ gateway service + healthcheck |
| `scripts/deploy/deploy.sh` / `rollback.sh` | ✓ present |
| Env sample keys | ✓ in `sample.env.production` |
| Compose context `../ReverseTunnelGateway` | ⚠ host layout requirement |

---

## 7. Files to **include** in ReverseTunnel-RC1 commit (Portal)

```
docker-compose.ra-production.yml
docs/deploy/ProductionDeploymentSteps.md
docs/deploy/README.md
docs/release/rc1/sample.env.production
docs/release/ReverseTunnel-RC1-Manifest.md
docs/GatewayArchitecture.md
docs/GatewayDeployment.md
docs/GatewayScaling.md
docs/MigrationGuide.md
docs/ReverseTunnelArchitecture.md
docs/ReverseTunnelCommissioning.md
docs/ReverseTunnelSAT.md
docs/ReverseTunnelSecurity.md
docs/ReverseTunnelTroubleshooting.md
docs/RemoteAnalysisPhase4LiveCommissioning.md
docs/RemoteAnalysisLiveCommissioning.md
docs/release/phase4/
iic_booking/remote_analysis/migrations/0015_reverse_tunnel_transport.py
iic_booking/remote_analysis/tunnel.py
iic_booking/remote_analysis/tunnel_models.py
iic_booking/remote_analysis/constants.py
iic_booking/remote_analysis/session_models.py
iic_booking/remote_analysis/models.py
iic_booking/remote_analysis/admin.py
iic_booking/remote_analysis/configuration_catalog.py
iic_booking/remote_analysis/guacamole/connection.py
iic_booking/remote_analysis/guacamole/settings_env.py
iic_booking/remote_analysis/operations/commissioning_observability.py
iic_booking/remote_analysis/operations/fault_injection.py
iic_booking/remote_analysis/operations/live_commissioning.py
iic_booking/remote_analysis/operations/live_commissioning_html.py
iic_booking/remote_analysis/operations/toolkit.py
iic_booking/remote_analysis/operations/toolkit_html.py
iic_booking/remote_analysis/operations/toolkit_views.py
iic_booking/remote_analysis/operations/views.py
iic_booking/remote_analysis/urls.py
iic_booking/remote_analysis/tests/test_reverse_tunnel.py
iic_booking/remote_analysis/tests/test_commissioning_toolkit.py
iic_booking/remote_analysis/tests/test_commissioning_observability.py
tests/analysis_platform/test_commissioning.py
```

## 8. Files to **exclude** from this RC commit

```
config/settings/base.py
config/settings/local.py
iic_booking/equipment/remote_analysis_integration/desktop_html.py
iic_booking/equipment/remote_analysis_integration/views.py
iic_booking/remote_analysis/services/reservation.py
iic_booking/remote_analysis/tests/test_booking_analysis_window.py
reports/
```

---

## 9. Docker images

| Image | Action |
|-------|--------|
| `iic_booking_production_django` | Rebuild |
| `reverse-tunnel-gateway` (compose build) | Build first time |
| Guacamole / guacd / postgres / redis | No rebuild required for RC |

---

## 10. Environment variables

| Variable | RC default for first prod deploy |
|----------|----------------------------------|
| `RA_TRANSPORT` | `direct_rdp` |
| `RA_TUNNEL_TOKEN_SECRET` | required if gateway started |
| `RA_TUNNEL_GATEWAY_ADMIN_KEY` | optional |
| `RA_TUNNEL_GATEWAY_ADMIN_URL` | `http://reverse-tunnel-gateway:7090/` |
| `RA_TUNNEL_GATEWAY_WSS_URL` | public WSS (unused until enable) |
| `RA_TUNNEL_ADAPTER_HOSTNAME` | `reverse-tunnel-gateway` |
| `RA_MOCK_GUACAMOLE` | `false` in production |

---

## 11. Health endpoints

| Endpoint | Role |
|----------|------|
| `/api/v1/analysis/health/live/` | Portal process |
| `/api/v1/analysis/health/ready/` | DB/cache/Guacamole/enrollment |
| `/api/v1/analysis/operations/toolkit/` + reverse tunnel probe | Ops |
| `/api/v1/analysis/operations/toolkit/live/` | Live commissioning |
| Gateway `GET …/api/v1/health` & `…/api/v1/metrics` | Via `TunnelGatewayClient` |

---

## 12. Feature flags

| Flag | Safe RC value |
|------|----------------|
| `transport_mode` / `RA_TRANSPORT` | `direct_rdp` |
| Enable reverse tunnel for users | **No** in this RC deploy |

---

## 13. Rollback procedure (after this RC is deployed)

1. Stop `reverse-tunnel-gateway` container.  
2. `ROLLBACK_REF=<pre-rc-sha> ./scripts/deploy/rollback.sh` **or** `git checkout <pre-rc-sha>` + rebuild django.  
3. If schema must be undone: restore DB backup taken before `migrate 0015`, or `migrate remote_analysis 0014` only if no dependent data.  
4. Confirm `RA_TRANSPORT=direct_rdp` and RA liveness 200.

---

## 14. Recommended commit (do not execute here)

**Title:**

```
Add Reverse Tunnel transport RC1 (Portal + compose + commissioning)
```

**Body:**

```
Introduce additive reverse-tunnel transport behind RA_TRANSPORT/direct_rdp default.

Includes TunnelSession models/migration 0015, Gateway client/orchestrator,
Guacamole adapter binding, compose reverse-tunnel-gateway service, toolkit
probes, live commissioning/fault-injection ops surfaces, and docs.

Does not enable reverse_tunnel for users. Gateway/Agent binaries remain
sibling packages. Excludes unrelated desktop CSRF and booking-window fixes.
```

**Ordered `git add`:**

```bash
git add docker-compose.ra-production.yml
git add docs/deploy/ProductionDeploymentSteps.md docs/deploy/README.md
git add docs/release/rc1/sample.env.production
git add docs/release/ReverseTunnel-RC1-Manifest.md
git add docs/GatewayArchitecture.md docs/GatewayDeployment.md docs/GatewayScaling.md docs/MigrationGuide.md
git add docs/ReverseTunnelArchitecture.md docs/ReverseTunnelCommissioning.md docs/ReverseTunnelSAT.md
git add docs/ReverseTunnelSecurity.md docs/ReverseTunnelTroubleshooting.md
git add docs/RemoteAnalysisPhase4LiveCommissioning.md docs/RemoteAnalysisLiveCommissioning.md
git add docs/release/phase4
git add iic_booking/remote_analysis/migrations/0015_reverse_tunnel_transport.py
git add iic_booking/remote_analysis/tunnel.py iic_booking/remote_analysis/tunnel_models.py
git add iic_booking/remote_analysis/constants.py iic_booking/remote_analysis/session_models.py
git add iic_booking/remote_analysis/models.py iic_booking/remote_analysis/admin.py
git add iic_booking/remote_analysis/configuration_catalog.py
git add iic_booking/remote_analysis/guacamole/connection.py iic_booking/remote_analysis/guacamole/settings_env.py
git add iic_booking/remote_analysis/operations/commissioning_observability.py
git add iic_booking/remote_analysis/operations/fault_injection.py
git add iic_booking/remote_analysis/operations/live_commissioning.py
git add iic_booking/remote_analysis/operations/live_commissioning_html.py
git add iic_booking/remote_analysis/operations/toolkit.py
git add iic_booking/remote_analysis/operations/toolkit_html.py
git add iic_booking/remote_analysis/operations/toolkit_views.py
git add iic_booking/remote_analysis/operations/views.py
git add iic_booking/remote_analysis/urls.py
git add iic_booking/remote_analysis/tests/test_reverse_tunnel.py
git add iic_booking/remote_analysis/tests/test_commissioning_toolkit.py
git add iic_booking/remote_analysis/tests/test_commissioning_observability.py
git add tests/analysis_platform/test_commissioning.py
```

**Expected `git status` after staging:** listed files staged; excluded files still modified/untracked; `reports/` untracked.

**Pre-commit checklist:**

- [ ] `git diff --cached --name-only` matches include list only  
- [ ] Exclusions still unstaged  
- [ ] `pytest` tunnel + commissioning suites green  
- [ ] Gateway/Agent builds noted in release notes  
- [ ] Manifest SHA placeholder ready to update after commit  

---

## 15. Release readiness table

| Item | Status |
|------|--------|
| Portal Ready | ✓ code complete (local) |
| Gateway Ready | ⚠ builds/tests pass locally; **not in Portal git** |
| Agent Ready | ⚠ builds/tests pass locally; **not in Portal git** |
| Database Ready | ✓ migration `0015` chain OK |
| Docker Ready | ✓ compose service present |
| Tests Passing | ✓ Portal RT/commissioning; Gateway 2; Agent 20 |
| Deployment Guide Ready | ✓ |
| Rollback Ready | ✓ scripts + procedure documented |
| Health Checks Ready | ✓ |
| Feature Flag Ready | ✓ defaults to `direct_rdp` |
| Production Deployment Ready | ✗ **blocked** — uncommitted; Gateway/Agent packaging; must exclude unrelated files first |
| **Overall Verdict** | **NOT READY** |

### Precise reasons for NOT READY

1. RC content is still only in the working tree — no commit SHA yet.  
2. Unrelated desktop CSRF / local CSRF / reservation-window changes are mixed in the tree and must stay out of this commit.  
3. Gateway and Agent are sibling trees without versioned release artifacts referenced by the Portal RC.  
4. Production Deployment Ready requires a pushed commit + host sibling Gateway — out of scope until you commit intentionally.
