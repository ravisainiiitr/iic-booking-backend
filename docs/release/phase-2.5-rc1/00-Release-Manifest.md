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
| Portal Backend | `iic-booking-backend` | `feature/forward-port-reverse-tunnel` | `4ed823579474a9b4d15ca35703543dfc42491184` | `4ed8235` |
| Portal Frontend | `iic-booking-frontend` | `main` | `e548c7962af84c611543b03e723ea76683e49476` | `e548c79` |
| Department Sync Agent | `DepartmentSyncAgent` | `recovery/dsa-phase-2.7` | `495e27b56377b1168328189ad82f2bfeee2be826` | `495e27b` |
| Equipment PC Wizard | *(same DSA repo or separate)* | `recovery/dsa-phase-2.7` | `495e27b56377b1168328189ad82f2bfeee2be826` | `495e27b` |
| Remote Analysis Agent | `RemoteAnalysis.Agent` | TBD | TBD | TBD |

**How to fill:** `git rev-parse HEAD` on clean release checkout; never use dirty working-tree SHAs.

### Commit progress (controlled creation)

| Order | Commit | Scope | SHA | Status |
|------:|--------|-------|-----|--------|
| 1 | B1 | Reverse Tunnel transport + orchestration | `d4d50e29891bce543d6d9258958fb744df71d90e` | Accepted |
| 2 | B2 | Remote Analysis execution engine | `500629b60992839fce99be2d2257230dfcb43ba3` | Accepted |
| 3 | B3 | Deployment Center | `24fb089613ad7fd51dd39bde24ebf1f2845a385d` | Accepted |
| 4 | B4 | Plug-and-Play Platform | `61b151fdb66d5dffef84dbbe9786e05e458ad167` | Accepted |
| 5 | B5 | Laboratory Infrastructure | `932d016bb1119e71ada4df4959ab508217d46c52` | Accepted |
| 6 | B6 | Diagnostics & Reporting | `49bfd66835e1c9d6d40e84184cf2dab28cd7281d` | Accepted |
| 7 | B7 | SAT Dashboard | `7b53a93542950ed30df8a27f235bfe7cfc02693d` | Accepted |
| 8 | B8 | Cross-cutting Stabilization | `4ed823579474a9b4d15ca35703543dfc42491184` | Accepted |
| 9 | F1 | Frontend Remote Analysis workspace lifecycle failure UX | `e8b4d1dd94f0fd79dbf11f8b3298d92b1b89e518` | Accepted |
| 10 | F2 | Frontend Deployment Center and Plug-and-Play UI | `3a66794e446374f65dcc939008c30f4f6aa1a7aa` | Accepted |
| 11 | F3 | Frontend Laboratory Infrastructure UI | `8cd1d59f7150b0b8354dce5dfc99b60ff8631056` | Accepted |
| 12 | F4 | Frontend SAT Dashboard and Reporting UI | `e548c7962af84c611543b03e723ea76683e49476` | Accepted |
| 13 | D0 | DSA Repository Recovery baseline | `b657c20228a9c7f273d78c0af6c6b25e059fa1f7` | Accepted |
| 14 | D1 | DSA Discovery and Provisioning control plane | `f58f8e5937c4f8e117d1af14b5e9ae01c9757b4e` | Accepted |
| 15 | D2 | DSA Configuration Platform and sync pipelines | `6c0191f1c7187ce005756264d9aa209c11546213` | Accepted |
| 16 | D3 | DSA Monitoring and Diagnostics platform | `6d9e5dd52ac80ceb564d947fba3fe16082e11224` | Accepted |
| 17 | D4 | DSA Documentation and Release assets | `495e27b56377b1168328189ad82f2bfeee2be826` | Accepted |

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
