# Platform RC1 Release Manifest

**Document type:** Platform Release Publication (Phase B0)  
**Status:** DRAFT — tags local only; not published; not deployed  
**Generated:** 2026-08-04  
**Authority:** Platform RC1 Freeze Certificate + local annotated tags

---

## Platform

| Field | Value |
|---|---|
| Platform Name | Institute Instrumentation Centre — Equipment Booking & Remote Analysis Platform |
| Platform Version | `2.5.0-rc1` |
| Release train model | Tags → Images + Installer Artifacts → Pull-Only Production |
| Overall freeze qualification | CONDITIONAL GO (deployment & live commissioning outstanding) |

---

## Repositories

| Component | Git remote (canonical) | Branch (freeze workstation) | Commit SHA | Annotated tag | Tag state |
|---|---|---|---|---|---|
| Portal Backend | `git@github.com:ravisainiiitr/iic-booking-backend.git` | `feature/forward-port-reverse-tunnel` (tracks `origin/master`, ahead locally) | **Docs/tag tip:** `c512199d61aac10a1155e7667dbb083d797fc481` · **Product baseline (B8):** `4ed823579474a9b4d15ca35703543dfc42491184` | `v2.5.0-rc1` | Local only |
| Frontend | `git@github.com:ravisainiiitr/iic-booking-frontend.git` | `main` (tracks `origin/master`, ahead 4) | `e548c7962af84c611543b03e723ea76683e49476` | `v2.5.0-rc1` | Local only |
| Department Sync Agent | `https://github.com/ravisainiiitr/DepartmentSyncAgent.git` | `recovery/dsa-phase-2.7` | `495e27b56377b1168328189ad82f2bfeee2be826` | `v1.0.0-rc1` | Local only |
| Remote Analysis Agent | `https://github.com/ravisainiiitr/RemoteAnalysisAgent.git` | `release/reverse-tunnel-rc1` (ahead 4) | `170d689e7e543f73e6b328ae6566ddddc57c0b1e` | `v1.0.0-rc1` | Local only |

### Backend dual-SHA note

- Tag `v2.5.0-rc1` points to **docs freeze tip** `c512199…`.
- Runtime product baseline remains **B8** `4ed8235…` (ancestor of the tag; intervening commits are documentation-only).
- Docker image builds SHOULD use checkout of tag `v2.5.0-rc1` (reproducible) or explicitly `4ed8235…` if policy requires product-only tree; record which was used in the deploy manifest.

---

## Compatibility Matrix

| Pair | Result | Basis |
|---|---|---|
| Backend ↔ Frontend | PASS | Mutual RC1 freeze; Frontend tag cites Backend `v2.5.0-rc1` |
| Backend ↔ DSA | PASS | DSA tag cites Backend `v2.5.0-rc1` |
| Backend ↔ RAA | PASS | RAA tag cites Backend `v2.5.0-rc1` |
| Frontend ↔ DSA | PASS | Portal-mediated |
| Frontend ↔ RAA | PASS | Portal-mediated |
| DSA ↔ RAA | PASS | Portal-mediated; RAA tag cites DSA `v1.0.0-rc1` |

---

## Compatible Installer Versions

| Artifact | Version string | Source tag / tip | Notes |
|---|---|---|---|
| Department Sync Agent | `1.0.0-rc1` | git `v1.0.0-rc1` @ `495e27b…` | VERSION file = `1.0.0-rc1` |
| Remote Analysis Agent | `1.0.0-rc1` (git) / legacy file `1.0.0-RT-RC1` | git `v1.0.0-rc1` @ `170d689…` | Deployment Center should publish git tag version; record legacy VERSION for field correlation |
| Equipment PC Configuration Wizard | `1.0.0-rc1` (planned DC version) | Built/published via Backend Deployment Center tooling against Backend RC1 | Exact installer SHA256 TBD at Batch 2/7 |

---

## Expected Docker Image Names & Tags

**Registry (planned):** `ghcr.io/ravisainiiitr` **or** AWS ECR in `ap-south-1` — choose one before Batch 3. Placeholders below use `REGISTRY`.

| Service | Local compose image name (current prod) | Planned registry image | Planned tag | Digest |
|---|---|---|---|---|
| Django | `iic_booking_production_django` | `REGISTRY/iic_booking_production_django` | `2.5.0-rc1` | `sha256:TBD` |
| Celery Worker | `iic_booking_production_celeryworker` | `REGISTRY/iic_booking_production_celeryworker` | `2.5.0-rc1` | `sha256:TBD` |
| Celery Beat | `iic_booking_production_celerybeat` | `REGISTRY/iic_booking_production_celerybeat` | `2.5.0-rc1` | `sha256:TBD` |
| Flower | `iic_booking_production_flower` | `REGISTRY/iic_booking_production_flower` | `2.5.0-rc1` | `sha256:TBD` |
| Frontend | `iic_booking_production_frontend` | `REGISTRY/iic_booking_production_frontend` | `2.5.0-rc1` | `sha256:TBD` |
| Reverse Tunnel Gateway | `reverse-tunnel-gateway` | `REGISTRY/reverse-tunnel-gateway` | `1.0.0-RT-RC1` (current prod) / align to RAA RC1 policy | `sha256:TBD` |
| Guacamole | `guacamole/guacamole` | upstream | `1.5.5` | pin digest at pull time |
| Guacd | `guacamole/guacd` | upstream | `1.5.5` | pin digest at pull time |
| Redis | `redis` | `docker.io/redis` | `7.2` | pin digest at pull time |
| Guacamole DB | `postgres` | `postgres` | `16-alpine` | pin digest at pull time |

**Build note:** Django/Celery/Celery Beat/Flower share `compose/production/django/Dockerfile` (different commands). Frontend uses `compose/production/Dockerfile`.

---

## Expected Deployment Center Versions

| Component | DC version label | Compatible platform |
|---|---|---|
| DSA installer | `1.0.0-rc1` | `2.5.0-rc1` |
| RAA installer | `1.0.0-rc1` | `2.5.0-rc1` |
| Equipment Wizard | `1.0.0-rc1` | `2.5.0-rc1` |
| Compatibility matrix entry | Platform `2.5.0-rc1` | All of the above |

---

## Expected Reverse Tunnel Gateway Version

| Field | Value |
|---|---|
| Current production image | `reverse-tunnel-gateway:1.0.0-RT-RC1` (`8be4084f8456` observed 2026-08-04) |
| RC1 policy | Retain `1.0.0-RT-RC1` for Backend RC1 unless a newer gateway build is certified against RAA `v1.0.0-rc1` |
| Compose overlays (prod) | `docker-compose.production.yml` + `docker-compose.rt-publish.yml` + `docker-compose.guacamole.yml` |

---

## Database Schema Versions (Backend product baseline `4ed8235…` / tag tree)

Critical app heads present in freeze tree:

| App | Highest migration number in freeze tree |
|---|---|
| `remote_analysis` | `0020` |
| `sync` | `0018` |
| `equipment` | `0184` |
| `deployment` | `0002` |
| `lab_infrastructure` | `0003` |
| `users` | `0095` |
| `communication` | `0052` |
| `support` | `0008` |
| `cms` | `0008` |
| `payments` | `0002` |
| `sites` | `0004` |

### Migration status (publication-time knowledge)

| Environment | Status |
|---|---|
| Freeze workstation | Schema defined by migrations in tagged tree |
| Production (`equip.iitr.ac.in` / RDS) as of 2026-08-04 audit | Live DB had applied history at least through `sync.0016`; **pending RC1 migrations expected** on deploy (exact pending list to be re-verified with `showmigrations` immediately before Batch 5) |
| Action at deploy | Backup → `migrate --plan` → `migrate --noinput` → verify heads |

Portal database: **AWS RDS** `iic-booking-rds.…ap-south-1.rds.amazonaws.com` (not containerized on EC2).

---

## Outstanding RC1 items (non-blocking for publication; track for acceptance)

1. Frontend `package.json` version still `0.0.0`
2. RAA VERSION file still `1.0.0-RT-RC1` vs git tag `v1.0.0-rc1`
3. Installer SHA256s not yet generated
4. Image digests not yet produced
5. Tags not pushed to GitHub
6. Live commissioning pending

---

## Document control

| Related | Path |
|---|---|
| Freeze certificate | `docs/release/Platform-RC1-Freeze-Certificate.md` |
| This manifest | `docs/release/Platform-RC1-Release-Manifest.md` |

**Do not deploy from this document until Release Gate Checklist is all PASS.**
