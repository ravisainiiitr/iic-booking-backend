# Deployment Audit Report — Phase 2 / 2.5 Visibility Gap

**Date:** 2026-08-04  
**Scope:** Why Main Admin cannot see Laboratory Infrastructure, Deployment Center, Acceptance Test Dashboard  
**Actions taken:** Audit only — **no commits, merges, or pushes**

---

## Executive finding (root cause)

**Production matches `origin/master`, which does not contain Phase 2 / Phase 2.5 UI or APIs.**

Those deliverables exist only in **local working trees** (staged / unstaged / untracked). They have **never been committed** and therefore cannot appear in any deploy that builds from `master`.

This is **not** primarily a permission, menu-registration, or feature-flag problem. It is **branch/deploy alignment**: working tree ≠ HEAD ≠ what production runs.

---

## STEP 1 — Repository audit

| Repository | Current branch | HEAD commit | origin/master (or default remote tip) | Divergence HEAD vs remote tip | Staged | Unstaged | Untracked | Notes |
|------------|----------------|-------------|----------------------------------------|-------------------------------|--------|----------|-----------|-------|
| **Portal Backend** `iic-booking-backend` | `feature/forward-port-reverse-tunnel` | `52ddcfc` — *fix(remote-analysis): allow launch when booking analysis window is already open* | `52ddcfc` (same) | **0 / 0** (identical commits) | **135** | **9** | **14** | Phase 2+ sits in index/WT only; feature branch tip == master tip |
| **Portal Frontend** `iic-booking-frontend` | `main` | `ffa5af4` — *fix(ui): show analysis launch gate errors…* | `ffa5af4` (tracks `origin/master`) | **0 / 0** | **0** | **6** | **4** | Lab/Deploy/SAT pages **untracked**; Dashboard/App routes only in unstaged diff |
| **Department Sync Agent** | **detached HEAD** at `54f1966` | `54f1966` — *Prepare repository for initial release* | Remote default is **`main`** (not `master`) | Detached from `main`/`develop`; large dirty tree | **~1318** | **26** | **~431** | Phase 1/2 DSA work largely uncommitted / not on a named branch tip |
| **Remote Analysis Agent** | `master` (no commits) | **none** | No usable remote history from this clone | N/A | **0** | **0** | **~78** | Entire agent tree is **untracked**; cannot push until initial commit |

### Interpretation

- Backend feature branch name suggests “forward port,” but **HEAD has no commits beyond `origin/master`**. All Phase 2 work is **uncommitted**.
- Frontend `main` == `origin/master`; Phase 2 pages are **local-only files**.
- Deploy workflow (backend): **push to `master`** → self-hosted `./deploy.sh`. Production therefore tracks **committed `master`**, not the dirty working tree.

---

## STEP 2 — Feature audit (current working tree)

| Feature | In working tree? | In HEAD / origin/master? | Evidence |
|---------|------------------|--------------------------|----------|
| Laboratory Infrastructure UI | **Yes** (`src/pages/LaboratoryInfrastructure.tsx`, Dashboard card, route) | **No** | Page **untracked**; Dashboard/App changes **unstaged** |
| Deployment Center UI | **Yes** (`DeploymentCenter.tsx` + route + card) | **No** | Same |
| Acceptance Test / SAT Dashboard UI | **Yes** (`TestDashboard.tsx` + route + card) | **No** | Same |
| Deployment APIs `/api/v1/deployment/` | **Yes** (`iic_booking/deployment/`, router include) | **No** | App + routes staged but **not in HEAD** (`git ls-tree HEAD` → 0 files) |
| Lab Infrastructure APIs `/api/v1/lab/` | **Yes** (fleet, repair, diagnostics, testing, …) | **No** | Same; SAT services/migrations **untracked** (`0002`, `0003`, `sat_execution.py`, `testing.py`) |
| Fleet Dashboard (Lab UI) | **Yes** (same as Lab Infrastructure) | **No** | — |
| Health / alerts APIs | **Yes** (`/api/v1/lab/alerts/`, `/testing/health/`) | **No** | — |
| Configuration Push + ack | **Yes** (lab + sync ack paths in WT) | **Partial / not on master as Phase 2** | Phase 2 ack/history in staged lab app; not in HEAD |
| Software Inventory / compliance | **Yes** (`software/compliance/`) | **No** | — |
| Diagnostics | **Yes** (node diagnostics endpoint + UI actions) | **No** | — |
| Reverse Tunnel | **Yes** in WT (`tunnel.py`, migrations, docs) | **Need confirm vs HEAD** — tunnel files are among staged changes; **not assumed on production master** without a commit that includes them | Staged under `remote_analysis/` |
| DSA discovery / EqPC / config pack | **Yes** in DSA WT | **Not on clean remote tip from this detached state** | Controllers often **untracked** |
| RAA update discover client | **Yes** in RAA WT | **No git history** | Untracked |

### Missing relative to “expected production”

**Everything Phase 2 / 2.5 that the Main Admin expects in the running app is missing from the deployable git tip (`origin/master`).**

Nothing critical is “missing from the working tree” for those three Dashboard cards — they are present locally and absent from what production can build.

---

## STEP 3 — Deployment audit (running production)

| Check | Result |
|-------|--------|
| Local `.envs/.production/.django` | **Not present** on this workstation |
| Frontend `.env.production` | **Not present** |
| Local Docker | **`docker` CLI not available** on this machine — cannot read container images/timestamps here |
| Backend CI/CD | `.github/workflows/deploy.yml`: **on push to `master`** → self-hosted runner → `/home/ubuntu/./deploy.sh` |
| Inferred production backend commit | **`origin/master` = `52ddcfc`** (same as local HEAD) → **no** `lab_infrastructure` / `deployment` apps |
| Inferred production frontend build | **`origin/master` = `ffa5af4`** → **no** Lab / Deployment Center / Test Dashboard routes or pages |
| Applied migrations (prod) | **Not verifiable from this laptop** — but migrations `deployment.*` / `lab_infrastructure.*` **do not exist on master**, so production **cannot** have applied them from git |
| Static / frontend bundle | Prod bundle built from master **cannot** include untracked pages |
| Runtime config | Not readable here |

**Conclusion:** Production is consistent with **git master**, and **inconsistent with the local Phase 2/2.5 working tree**. That explains the missing cards.

*To complete Step 3 on the server (operator):* `git rev-parse HEAD` in deploy dirs, image tags/created, `showmigrations deployment lab_infrastructure`, and confirm frontend artifact build SHA.

---

## STEP 4 — Permission audit (Main Administrator visibility)

| Layer | Finding |
|-------|---------|
| **Dashboard cards** | Gated by `isAdmin = (userTypeStr === 'admin')` only — **no** separate feature flag |
| **Routes** | `/laboratory-infrastructure`, `/deployment-center`, `/test-dashboard` registered only in **working-tree** `App.tsx` |
| **Menu registration** | Not a separate CMS menu — **Dashboard quick-access cards** + routes |
| **Backend Lab APIs** | Mostly `CanManageDepartmentSync`; SAT/testing endpoints additionally require **`user_type == admin` or superuser** |
| **Why prod Admin sees nothing** | Cards/routes **not in the deployed frontend bundle**. Even a deep-link would hit a SPA without those routes; APIs would **404** on backend master |

**If** after a correct deploy an Admin still cannot see cards: verify `user.user_type === "admin"` (not only staff/superuser / dept admin). That is a secondary check — **not** the current production failure mode.

---

## STEP 5 — Branch strategy recommendation

**Recommend Option B — `release/phase-2.5` (or `release/phase2-sat`)**

| Option | Pros | Cons |
|--------|------|------|
| **A** Continue on `feature/forward-port-reverse-tunnel` → SAT → merge master | Branch already named | Tip currently **equals** master with **no commits**; easy to confuse dirty WT with “branch work”; weaker release boundary |
| **B** Create `release/phase-2.5` from current master, commit Phase 1–2.5 groups onto release, Lab SAT against staging built from release, then merge release → master | Clear release candidate; matches deploy-on-master workflow; easier rollback tag; SAT signs off one SHA | One-time branch create + disciplined commits |

**Safest path:**

1. Keep SAT gate closed (no production push).  
2. After explicit approval to **commit** (not push): create `release/phase-2.5` from `origin/master`.  
3. Apply logical commit groups (Step 7) onto release.  
4. Deploy **staging** from release; run Lab SAT.  
5. Only after readiness **GO**: merge release → `master` (triggers prod deploy) with tag.

Also: put DSA back on `develop` or a `release/phase-2.5` branch; create RAA initial history on `main`/`master` before any agent release.

---

## STEP 6 — Pre-production deployment checklist

Do **not** promote until each item is done:

- [ ] **Commits exist** on a release branch containing frontend pages + Dashboard/App + backend apps (today: **missing**)
- [ ] **Frontend production build** from that SHA (pages no longer untracked)
- [ ] **Backend image rebuild** from that SHA (`deployment` + `lab_infrastructure` in `INSTALLED_APPS`)
- [ ] **Migrations applied:** `deployment`, `lab_infrastructure` (0001–0003 as applicable), plus any sync/RA heads from the release
- [ ] **collectstatic** / CDN refresh for Django admin/static if required
- [ ] **Celery beat** includes `lab_infrastructure` health detectors (if used)
- [ ] **Smoke:** Main Admin Dashboard shows three cards; routes load; `GET /api/v1/deployment/…` and `GET /api/v1/lab/infrastructure/` return 200 (not 404)
- [ ] **Permissions smoke:** `user_type=admin` sees cards; non-admin does not
- [ ] **Lab SAT** executed; Critical=0; High=0; readiness **GO**
- [ ] **Explicit approval** to merge release → `master` / push
- [ ] DSA/RAA installers rebuilt from committed agent SHAs if lab depends on them

---

## STEP 7 — Proposed commit groups (DO NOT CREATE YET)

Suggested order for `release/phase-2.5` (adjust after `git status` review at commit time):

| # | Proposed message theme | Contents (intent) |
|---|------------------------|-------------------|
| 1 | Reverse tunnel / RA session path | `remote_analysis` tunnel, guacamole-related, RA migrations/docs already staged for tunnel |
| 2 | Deployment Center (backend) | `iic_booking/deployment/*`, router/settings includes, publish commands |
| 3 | Plug-and-play / DSA portal sync hooks | sync serializers equipment_pcs, bootstrap fixes, templates/IP soft reservation pieces |
| 4 | Fleet / Lab Infrastructure APIs | `lab_infrastructure` app 0001, fleet/alerts/repair/diagnostics/config ack |
| 5 | Lab SAT Execution Dashboard (backend) | testing models 0002/0003, `sat_execution`, `testing` services, testing URLs |
| 6 | Frontend: Deployment Center + Lab Infra + SAT UI | `DeploymentCenter.tsx`, `LaboratoryInfrastructure.tsx`, `TestDashboard.tsx`, `App.tsx`, `Dashboard.tsx`, `api.ts` |
| 7 | Documentation Phase 1/2/2.5 | `docs/plug-and-play`, `docs/enterprise`, `docs/phase-2.5`, `Documentation/*` |
| 8 | Stabilization defect fixes | Remaining Phase 2.5 reliability/security fixes if not already in groups 3–5 |
| — | **DSA** (separate repo) | Discovery, pairing, EqPC status, config ack, OTP/H-01/H-04 fixes on a proper branch |
| — | **RAA** (separate repo) | Initial commit + heartbeat enrichment / update discover (separate history) |

Exact file lists should be regenerated with `git status` / `git diff --cached` immediately before each approved commit.

---

## STEP 8 — STOP

**Stopped.** No commits, merges, or pushes performed.

### Why Admin cannot see the three modules (one sentence)

**Because production is built from `origin/master`, and Laboratory Infrastructure, Deployment Center, and the Acceptance Test Dashboard exist only as uncommitted local frontend/backend files.**

Awaiting explicit approval before any commit or promotion work.
