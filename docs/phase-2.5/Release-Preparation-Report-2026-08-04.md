# Release Preparation Report — Clean Git History Plan

**Date:** 2026-08-04  
**Mode:** Planning only — **no staging, commits, branches, merges, or pushes**  
**SAT gate:** Unchanged (still closed for production)

---

## Summary

| Repo | Dirty scale | Ready to commit as-is? |
|------|-------------|------------------------|
| Portal Backend | ~151 porcelain entries (135 staged + unstaged/untracked) | **No** — unstage/regroup; drop junk; fix migration packaging |
| Portal Frontend | 13 entries (9 modified + 4 untracked pages) | **Almost** — small; split Phase 2 UI vs unrelated GPS/vite diffs |
| Department Sync Agent | ~1752 porcelain; detached HEAD; 383 `artifacts/` untracked | **No** — massive; exclude build artifacts; re-attach to a branch later |
| Remote Analysis Agent | 78 untracked; **no commits ever** | **No** — initial history needed; exclude `data/*.db`, `bin/`, `obj/`, tmp |

---

# STEP 1 — Working tree inventory

## 1.1 Portal Backend (`iic-booking-backend-rt-port`)

| Category | Count (approx) | Notes |
|----------|----------------|-------|
| Staged **Added** | 91 | New apps, migrations, docs, tests |
| Staged **Modified** | 44 | Existing RA/sync/equipment/config |
| Unstaged **Modified** | 9 | Lab SAT + installer discover overlays on staged files |
| Untracked | 15 | Phase 2.5 docs + SAT migrations/services |
| Deleted / Renamed | 0 | None observed |

### By module (working tree — includes staged + unstaged + untracked)

#### Config / wiring
| State | Path |
|-------|------|
| M | `config/api_router.py`, `config/settings/base.py` |

#### Reverse Tunnel + Remote Analysis core
| State | Paths |
|-------|------|
| A | `iic_booking/remote_analysis/tunnel.py`, `tunnel_models.py` |
| A | migrations `0017`–`0020` |
| A | `services/checkin.py`, `fleet_inventory.py`, `maintenance.py`, `production_commissioning.py`, `workstation_identity.py` |
| A | tests `test_reverse_tunnel.py`, `test_maintenance_mode.py`, `test_end_analysis_and_software_alloc.py` |
| A | cmds `publish_ra_installer.py`, `verify_reverse_tunnel_production.py` |
| M | guacamole/*, `models.py`, `views.py`, `urls.py`, `serializers.py`, heartbeat/health/scheduler/registration/commands, `session_models.py`, `tasks.py`, `admin.py`, `constants.py`, `configuration_catalog.py`, workspace sync |
| M (unstaged) | `installer/views.py`, `urls.py` (agent update discover/report) |

#### Equipment / booking RA integration
| State | Paths |
|-------|------|
| A | equipment migrations `0182`–`0184` |
| M | `equipment/models.py`, `serializers.py`, `equipment_addition_requests.py`, `remote_analysis_integration/*` |

#### Deployment Center (backend)
| State | Paths |
|-------|------|
| A | entire `iic_booking/deployment/` (models, views, urls, admin, migrations 0001–0002, publish_equipment_wizard) |
| M | `installer_download_tickets.py` (shared tickets) |

#### Plug-and-Play / DSA portal (sync)
| State | Paths |
|-------|------|
| A | sync migrations `0017` template, `0018` IP reservation |
| A | `sync/admin/templates_admin.py`, `ip_reservations.py`, apply template HTML |
| A | `publish_dsa_installer.py` |
| M | `sync/models.py`, `serializers.py`, `bootstrap.py`, `heartbeat.py`, `urls.py`, `admin/__init__.py` |

#### Laboratory Infrastructure / Fleet
| State | Paths |
|-------|------|
| A | `lab_infrastructure/` app (0001, fleet, detectors, tasks, repair, views, urls, tests) |
| AM / M | models/views/urls/admin (SAT fields layered on top) |

#### SAT Dashboard (backend)
| State | Paths |
|-------|------|
| ?? | `migrations/0002_sat_test_dashboard.py`, `0003_sat_execution_mode.py` |
| ?? | `services/testing.py`, `sat_execution.py`, `seed_sat_catalog.py` |
| M | views/urls/models/admin for testing APIs |

#### Documentation
| State | Paths |
|-------|------|
| A | `docs/ReverseTunnel*.md`, `docs/plug-and-play/*`, `docs/enterprise/*`, `docs/phase-2.5/README.md` |
| A | `Documentation/*` new guides + ProductionReadiness RA |
| M | AnalysisScheduler, RemoteAnalysisAgent, TroubleshootingGuide |
| ?? | remaining `docs/phase-2.5/*` plans + Deployment-Audit |
| M (unstaged) | enterprise AgentUpdates/README, phase-2.5 README |

#### Temporary / do-not-ship
| State | Path |
|-------|------|
| A **staged** | `tmp_commission_run.py` ← **remove from commit plan** |

---

## 1.2 Portal Frontend (`iic-booking-frontend`)

| Category | Count | Files |
|----------|-------|-------|
| Modified (unstaged) | 9 | See below |
| Untracked | 4 | New pages |
| Staged / Deleted / Renamed | 0 | — |

### Grouped

| Module | Files |
|--------|-------|
| **Deployment Center UI** | `?? DeploymentCenter.tsx`; routes/cards/api portions of `App.tsx`, `Dashboard.tsx`, `api.ts` |
| **Lab Infrastructure / Fleet UI** | `?? LaboratoryInfrastructure.tsx`; Dashboard card; api lab_* |
| **SAT / Acceptance Test Dashboard** | `?? TestDashboard.tsx`; Dashboard card; api testing_* |
| **RA diagnostics UI** | `?? RdpPathDiagnostics.tsx`; related `RemoteAnalysis.tsx` / route if any |
| **RA launch UX** | `M AnalysisLaunch.tsx` |
| **Ancillary (likely unrelated GPS / chrome)** | `M BackToDashboardButton.tsx`, `EquipmentLocationFields.tsx`, `equipmentGps.ts`, `vite.config.ts` — **review separately**; do not mix into Phase 2 commits without intent |

---

## 1.3 Department Sync Agent

| Category | Approx | Notes |
|----------|--------|-------|
| Staged | ~1318 | Large agent platform rewrite vs `54f1966` |
| Unstaged | 26 | Heartbeat rollup, Program.cs, etc. |
| Untracked | ~431 | **383 under `artifacts/dsa-installer/`** + Phase1 EqPC/Wizard/Installer source + docs |
| Branch | **detached HEAD** | Must reattach before committing (later approval) |

### Module sketch (source worth committing later)

| Module | Examples |
|--------|----------|
| Core DSA platform | Api/Application/Infrastructure staged mass |
| **DSA Discovery / EqPC** | `EquipmentPcControllers.cs`, filters, `EquipmentPcServices`, entities, EF migrations `20260803*`, `20260804*` |
| **Config ack** | `ConfigurationAckService`, ack DTOs |
| **Equipment Wizard** | `EquipmentPcConfigurationWizard/**` |
| **DSA Installer (source)** | `DepartmentSyncAgent.Installer/**` (source only) |
| Docs | `docs/PlugAndPlayPhase1.md`, `EnterpriseLifecyclePhase2.md`, scripts |

---

## 1.4 Remote Analysis Agent

| Category | Count | Notes |
|----------|-------|-------|
| Untracked source | ~70+ | Full agent |
| Untracked junk | `data/RemoteAnalysis.db*`, `tmp-end-analysis-diff.txt`; `bin/`/`obj/` present on disk but **gitignore** covers them |
| Commits | **None** | First commit(s) create history |

### Module sketch

| Module | Paths |
|--------|-------|
| Agent core | Program, HostedServices, Portal client, inventory, workspace, commands |
| Reverse tunnel probe | `ReverseTunnelStatusProbe.cs` |
| Update discover | `UpdateDiscoveryHostedService`, `InstallerReleaseClient` |
| Diagnostics | `PostInstallDiagnostics`, `DiagnosticController` |
| Docs | `Documentation/*`, README |
| Ignore | `.gitignore` |

---

# STEP 2 — Junk / never commit (list only — **not deleted**)

## Backend
| Item | Reason |
|------|--------|
| `tmp_commission_run.py` (**currently staged**) | Ad-hoc script |
| `__pycache__/`, `*.pyc` | Generated |
| Local `.envs/.production` secrets (if appear) | Secrets |
| IDE / `.coverage` / pytest cache if present | Local |

## Frontend
| Item | Reason |
|------|--------|
| `node_modules/`, `dist/` | Build (normally gitignored) |

## DSA
| Item | Reason |
|------|--------|
| **`artifacts/dsa-installer/**` (~383 files)** | Installer **build output** — extend `.gitignore` to `artifacts/` if not already ignored for untracked |
| `**/bin/`, `**/obj/`, `publish/`, `Frontend/node_modules/`, `Frontend/dist/` | Already in `.gitignore`; ensure never force-added |
| `.vs/`, `.idea/` | IDE |

## RAA
| Item | Reason |
|------|--------|
| `src/.../data/RemoteAnalysis.db*` | Local SQLite runtime (**gitignore**) but currently listed untracked if ignore not applied yet — confirm before add |
| `tmp-end-analysis-diff.txt` | Temp diff |
| `bin/`, `obj/` | Build |

**Action later (approval):** unstage `tmp_commission_run.py`; add `artifacts/` to DSA gitignore if needed; never `git add -f` build trees.

---

# STEP 3 — Feature groups (each file → one group)

| ID | Group | Primary repos |
|----|-------|----------------|
| G1 | Reverse Tunnel | Backend RA tunnel + docs ReverseTunnel*; RAA probe |
| G2 | Remote Analysis lifecycle (maintenance, fingerprint, check-in, commissioning, fleet inventory APIs) | Backend RA + equipment 0182–0184 |
| G3 | Deployment Center | Backend `deployment/` + FE DeploymentCenter + publish cmds wiring |
| G4 | Plug-and-Play / Config templates / Soft IP | Backend sync 0017–0018 + admin; DSA discovery/EqPC/Wizard |
| G5 | Laboratory Infrastructure / Fleet | Backend `lab_infrastructure` 0001 + fleet/detectors; FE LaboratoryInfrastructure |
| G6 | Configuration Push / Ack / Software compliance / Diagnostics / Reporting (lab APIs) | Lab views subsets + DSA ack service |
| G7 | SAT / Acceptance Test Dashboard | Lab 0002–0003 + sat_execution/testing; FE TestDashboard |
| G8 | Agent installers / update discover | Backend installer views/urls; RAA UpdateDiscovery; DSA Installer **source** |
| G9 | Documentation | docs/* Documentation/* phase-2.5 plans |
| G10 | Tests | `**/tests/**` colocated with feature commits preferred |
| G11 | Ancillary frontend | GPS / BackToDashboard / vite — **park or separate tiny commit** |
| G12 | Stabilization bugfixes | Serializer equipment_pcs, bootstrap last_reported, pairing fail-closed, OTP strip, etc. if not already inside G4–G7 |

Shared wiring (`api_router.py`, `settings/base.py`, `App.tsx`, `Dashboard.tsx`, `api.ts`) must be **split carefully per commit** or landed in the first commit that introduces the app, then amended only by later feature commits touching the same lines (prefer sequential commits that add routes incrementally).

---

# STEP 4 — Dependency graph → commit order

```text
G1 Reverse Tunnel
  └─► G2 RA lifecycle / equipment RA fields
        └─► G3 Deployment Center (portal distribution)
              └─► G4 Plug-and-Play (sync templates/IP + DSA EqPC)
                    └─► G5 Lab Infrastructure / Fleet
                          └─► G6 Config push UI/API completeness + diagnostics/reporting surfaces
                                └─► G7 SAT Dashboard
                                      └─► G8 Agent update discover / installer polish
                                            └─► G9 Documentation
                                                  └─► G12 residual bugfixes (only if not folded earlier)
G11 ancillary FE — independent; commit last or omit from release/phase2.5
```

**Migration order (must match commits):**

1. `equipment` 0182 → 0183 → 0184  
2. `remote_analysis` 0017 → 0018 → 0019 → 0020  
3. `sync` 0017 → 0018  
4. `deployment` 0001 → 0002 (independent app; can parallel after settings include)  
5. `lab_infrastructure` 0001 → 0002 → 0003 (**requires sync 0018**)

**Note:** HEAD already contains `remote_analysis.0015_reverse_tunnel_transport` and `0016_agent_installer_release`. New `0017_restore_*` is an idempotent restore — review carefully on prod DBs that already applied older tunnel schemas.

---

# STEP 5 — Migration review

| Migration | Deps | Recommendation |
|-----------|------|----------------|
| RA `0017_restore_reverse_tunnel_transport` | RA 0016 + equipment 0181 | **Keep**; verify idempotent on DBs with full 0015; document runbook; **do not fake** unless schema already matches |
| RA 0018–0020 | Chain from 0017 | OK |
| sync 0017–0018 | from 0016 | OK; lab 0001 needs 0018 |
| equipment 0182–0184 | from 0181 | OK; RA 0017 also lists equipment 0181 — ensure 0182+ applied in deploy order before relying on new fields |
| deployment 0001 | **empty deps** | Prefer adding `settings.AUTH_USER_MODEL` if models need it; OK for isolated app |
| deployment 0002 | 0001 | OK |
| lab 0001 | sync 0018 | OK |
| lab 0002–0003 | 0001 | **Currently untracked** — must be in same release as SAT models in `models.py` (avoid model/migration drift) |
| DSA EF `20260803*`, `20260804*` | local EF | Commit with EqPC feature; ensure snapshot consistency |

**Risks**
- Staged `lab_infrastructure/models.py` already contains SAT models while `0002`/`0003` untracked → **migrate will fail** if app loads models without migrations. Before any commit of models, either include 0002/0003 in the same commit as model SAT fields, or temporarily keep SAT models only with their migrations.
- `0017_restore` vs historical `0015` on production: follow existing comments; test on staging dump.
- No duplicate migration numbers observed in new chains.

**Rollback:** Django reverse for lab/deployment/sync usually OK; RA 0017 may be partial/idempotent — treat as **forward-fix only** in runbook if reverse is unsafe.

---

# STEP 6 — Test review / missing coverage

| Area | Present | Gap |
|------|---------|-----|
| Reverse tunnel | Strong `test_reverse_tunnel.py` | Keep in G1 |
| Maintenance | `test_maintenance_mode.py` | OK |
| End analysis / software | `test_end_analysis_and_software_alloc.py` | OK |
| Lab infrastructure | Smoke tests in `test_lab_infrastructure.py` | Missing: config push/rollback, repair command branches, SAT wizard/evidence/defects APIs, readiness scoring |
| Deployment Center | **No** dedicated tests | Add API smoke (list center, authz) |
| Sync templates / IP | Existing sync security tests may not cover new models | Add template apply + IP reservation tests |
| Frontend | No unit tests observed for new pages | SAT UI covered by Lab SAT manual; optional smoke |
| DSA | Integration workflow exists in tree | Ensure EqPC pairing/status tests before release |
| RAA | Unknown automated suite in tree | At least build + smoke registration |

**SAT coverage:** Catalog exists in `testing.py` / docs — maps to manual Lab SAT, not pytest.

---

# STEP 7 — Code quality (Keep / Delete / Review)

| Item | Verdict |
|------|---------|
| `tmp_commission_run.py` | **Delete** from index (do not commit) |
| DSA `artifacts/dsa-installer/**` | **Delete** from commit consideration; ignore |
| RAA `tmp-end-analysis-diff.txt`, `*.db*` | **Delete** / ignore |
| `Documentation/remote-analysis-agent.canvas.tsx` | **Review** — Cursor canvas; optional docs-only |
| Duplicate fleet concepts (RA `fleet_inventory` vs lab fleet) | **Keep** both — different control planes; document |
| Staged vs unstaged dual versions of lab views | **Review** — ensure final commit content includes SAT endpoints |
| Frontend GPS / vite diffs | **Review** — exclude from Phase 2 release unless intentional |
| DSA detached HEAD + 1300 staged files | **Review** — may include pre-Phase1 platform; split “DSA core catch-up” vs “Phase1 discovery” commits |
| Empty/duplicate serializers | Spot-check at commit time |

---

# STEP 8 — Production-quality commit plan (proposal only)

## Portal Backend (suggested 10 commits)

| # | Title | Includes | Must compile/migrate |
|---|-------|----------|----------------------|
| B1 | `feat(remote-analysis): reverse tunnel transport restore and session path` | tunnel*, 0017, guacamole/session pieces needed for tunnel, tests reverse tunnel, ReverseTunnel docs | yes |
| B2 | `feat(remote-analysis): maintenance, fingerprint, check-in, commissioning, fleet inventory` | 0018–0020, services, tests maintenance/end-analysis, related model/view changes | yes |
| B3 | `feat(equipment): analysis session duration, raw/results dirs, check-in policy` | 0182–0184 + equipment model/serializer/integration | yes |
| B4 | `feat(deployment): Deployment Center API and wizard releases` | `deployment/` app, settings/router hooks for deployment, publish_equipment_wizard, tickets touch if required | yes |
| B5 | `feat(sync): equipment sync templates and soft IP reservation` | 0017–0018, templates admin, bootstrap/heartbeat/serializers for plug-and-play | yes |
| B6 | `feat(lab): Laboratory Infrastructure fleet, alerts, repair, diagnostics` | lab app **0001 only** + fleet/detectors/tasks + views **without SAT** if splittable; else B6+B7 combined | yes |
| B7 | `feat(lab): SAT execution dashboard APIs, evidence, defects, reports` | 0002–0003, sat_execution, testing, seed command, testing URLs | yes |
| B8 | `feat(remote-analysis): agent update discover/report endpoints` | installer views/urls unstaged | yes |
| B9 | `docs: plug-and-play, enterprise, phase-2.5 acceptance plans` | docs trees + Documentation guides | n/a |
| B10 | `fix: Phase 2.5 stabilization (heartbeat equipment_pcs, config persist, bootstrap ack)` | only residual fixes not already in B5–B7 | yes |

## Portal Frontend (suggested 4–5 commits)

| # | Title | Includes |
|---|-------|----------|
| F1 | `feat(ui): Deployment Center page and dashboard entry` | DeploymentCenter.tsx + App/Dashboard/api slices |
| F2 | `feat(ui): Laboratory Infrastructure fleet dashboard` | LaboratoryInfrastructure.tsx + slices |
| F3 | `feat(ui): Lab SAT / Acceptance Test Dashboard` | TestDashboard.tsx + slices |
| F4 | `feat(ui): RDP path diagnostics` | RdpPathDiagnostics + RemoteAnalysis/AnalysisLaunch if tied |
| F5 | `chore(ui): GPS / vite / back-button` | **optional** — only if approved as intentional |

## DSA (suggested after reattach to branch)

| # | Title |
|---|-------|
| D1 | Core agent platform catch-up (non-EqPC) — may need further splitting after file review |
| D2 | Equipment PC discovery, pairing, config-pack, status, IP |
| D3 | Configuration ack + heartbeat `equipment_pcs` rollup |
| D4 | Equipment PC Configuration Wizard (source) |
| D5 | DSA Installer **source** only (no artifacts) |
| D6 | Docs Phase1/Phase2 |

## RAA (suggested)

| # | Title |
|---|-------|
| R1 | `chore: initial Remote Analysis Agent skeleton and .gitignore` |
| R2 | `feat: registration, heartbeat, inventory, workspace sync` |
| R3 | `feat: reverse tunnel status probe and diagnostics` |
| R4 | `feat: update discovery client` |
| R5 | Docs |

Each commit: related files only; run targeted tests/build before the next.

---

# STEP 9 — Release plan (after commits exist — not now)

```text
origin/master (current production tip)
        │
        ▼
  release/phase-2.5   ← create ONLY after commit approval
        │  (B1…B10, F1…F4, tag agents)
        ▼
  Staging deploy + migrate (order in §4)
        │
        ▼
  Lab SAT Execution Mode → readiness GO
        │
        ▼
  merge release/phase-2.5 → master
        │
        ▼
  tag: portal-vX.Y.Z / dsa-v… / raa-v…
        │
        ▼
  Production deploy (master workflow)
```

| Topic | Recommendation |
|-------|----------------|
| **Tag** | `release/phase-2.5` annotated tag on merge commit; agent installer versions aligned in Deployment Center |
| **Release notes** | Phase 1 PnP + Phase 2 fleet/lifecycle + Phase 2.5 SAT tooling; list migrations; known High H-06/H-10/H-11 |
| **Migration order** | equipment → remote_analysis → sync → deployment → lab_infrastructure |
| **Deploy order** | 1) Backend migrate+restart 2) Frontend rebuild/publish 3) Celery beat 4) DSA/RAA lab upgrades from Deployment Center |
| **Rollback** | Revert merge tag; restore DB backup taken pre-migrate; frontend previous artifact; agents remain backward compatible where possible |

---

# STEP 10 — STOP

**No commits, staging changes, branches, merges, or pushes performed.**

### Immediate human actions before approval to commit
1. Confirm GPS/vite frontend diffs are in or out of Phase 2.5.  
2. Confirm `tmp_commission_run.py` and DSA `artifacts/` stay out.  
3. Decide whether lab models+SAT migrations ship as one commit (B6+B7) to avoid migration drift.  
4. Plan DSA: leave detached HEAD until explicit “create branch + commit” approval.

Awaiting manual review and explicit approval before any Git history is created.
