# Release Manifest — Template

**Release name:** IIC Laboratory Platform Phase 2.5  
**Candidate:** RC1  
**Status:** TEMPLATE — fill only from measured values after commits/tags exist  

> Nothing below may be invented. Use `TBD` until a SHA, hash, or version is recorded from CI/build output.

---

## Identity

| Field | Value |
|-------|-------|
| Release version (platform) | `2.5.0-rc1` *(proposed — confirm)* |
| Release date (UTC) | TBD |
| Release manager | TBD |
| Git tag (portal monorepo / backend) | TBD e.g. `platform-v2.5.0-rc1` |
| Prior production baseline | Backend `52ddcfc` / Frontend `ffa5af4` on `origin/master` *(audit 2026-08-04)* |
| Rollback version / tag | TBD (previous prod tag) |

---

## Source commits (fill after history exists)

| Component | Repository | Branch / tag | Commit SHA (full) | Short |
|-----------|------------|--------------|-------------------|-------|
| Portal Backend | `iic-booking-backend` | `feature/forward-port-reverse-tunnel` | `d4d50e29891bce543d6d9258958fb744df71d90e` | `d4d50e2` |
| Portal Frontend | `iic-booking-frontend` | TBD | TBD | TBD |
| Department Sync Agent | `DepartmentSyncAgent` | TBD | TBD | TBD |
| Equipment PC Wizard | *(same DSA repo or separate)* | TBD | TBD | TBD |
| Remote Analysis Agent | `RemoteAnalysis.Agent` | TBD | TBD | TBD |

**How to fill:** `git rev-parse HEAD` on clean release checkout; never use dirty working-tree SHAs.

### Commit progress (controlled creation)

| Order | Commit | Scope | SHA | Status |
|------:|--------|-------|-----|--------|
| 1 | B1 | Reverse Tunnel transport + orchestration | `d4d50e29891bce543d6d9258958fb744df71d90e` | Accepted |
| 2 | B2 | Remote Analysis execution engine | `500629b60992839fce99be2d2257230dfcb43ba3` | Accepted |
| 3 | B3 | Deployment Center | `24fb089613ad7fd51dd39bde24ebf1f2845a385d` | Accepted |
| 4 | B4 | Plug-and-Play Platform | `TBD (assigned after commit)` | In progress |

---

## Database

| Field | Value |
|-------|-------|
| Schema label | `2.5.0` *(logical)* |
| Django migration heads (record `showmigrations --plan` tail) | TBD |
| `equipment` head | TBD (expect ≥ `0184_…` on RC) |
| `remote_analysis` head | TBD (expect ≥ `0020_…`) |
| `sync` head | TBD (expect ≥ `0018_…`) |
| `deployment` head | TBD (expect `0002_…`) |
| `lab_infrastructure` head | TBD (expect `0003_…`) |
| Pre-upgrade backup ID / path | TBD |
| Post-upgrade verification | TBD |

---

## Docker / runtime images

| Image / service | Tag / digest | Built from commit | Build date (UTC) |
|-----------------|--------------|-------------------|------------------|
| Portal Django | TBD | TBD | TBD |
| Portal Frontend (nginx) | TBD | TBD | TBD |
| Celery worker | TBD | TBD | TBD |
| Celery beat | TBD | TBD | TBD |
| Postgres | TBD (pinned) | — | — |
| Redis | TBD (pinned) | — | — |
| Guacamole / guacd | TBD | — | — |

**Compose file used:** TBD (`docker-compose.ra-production.yml` / `docker-compose.production.yml`)

---

## Installers (Deployment Center)

| Product | Version | Filename | Size | SHA-256 | Signature status | Published by |
|---------|---------|----------|------|---------|------------------|--------------|
| DSA | TBD | TBD | TBD | TBD | TBD | TBD |
| RAA | TBD | TBD | TBD | TBD | TBD | TBD |
| Equipment PC Wizard | TBD | TBD | TBD | TBD | TBD | TBD |

**How to fill:** `Get-FileHash -Algorithm SHA256`; portal `publish_*` command output; Authenticode if used.

---

## Compatibility matrix (min versions)

| Consumer | Requires Portal API | Requires DSA | Requires RAA | Requires Wizard | Notes |
|----------|---------------------|--------------|--------------|-----------------|-------|
| Portal Frontend 2.5.x | TBD | — | — | — | |
| DSA 1.0.x | TBD | — | — | — | heartbeat `equipment_pcs`, config ack |
| Wizard 1.0.x | — | TBD | — | — | discovery/pairing |
| RAA 1.0.x | TBD | — | — | — | enrollment + update discover |

*(Copy from Deployment Center `compatibility` JSON once published.)*

---

## Release notes / known issues

| Artifact | Link / path | Status |
|----------|-------------|--------|
| Release Notes | [08-Release-Notes.md](./08-Release-Notes.md) | Draft |
| Known Issues | [09-Known-Issues.md](./09-Known-Issues.md) | Draft |
| Upgrade Guide | [10-Upgrade-Guide.md](./10-Upgrade-Guide.md) | Draft |
| Rollback Plan | [05-Rollback-Plan.md](./05-Rollback-Plan.md) | Draft |
| Change Log | [11-Change-Log.md](./11-Change-Log.md) | Draft |

---

## Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Release Manager | | | |
| Portal lead | | | |
| Lab SAT lead | | | |
| Security reviewer | | | |
| Approver (GO) | | | |
