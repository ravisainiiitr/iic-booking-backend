# Repository Health Reports — Phase 2.6

**Assessment date:** 2026-08-04  
**Scoring:** 0–100 (releasable hygiene, not product quality).  
**Components:** Portal Backend, Portal Frontend, DSA, Equipment Wizard *(lives in DSA repo)*, RAA.

### Score rubric (abbreviated)

| Band | Meaning |
|------|---------|
| 80–100 | Clean tip, history, CI, ignore rules, rebuildable |
| 60–79 | Recoverable with standard hygiene |
| 40–59 | Major blockers (dirty tip / missing CI) |
| 0–39 | Not version-controllable as a release input |

---

## 1. Portal Backend (`iic-booking-backend`)

| Metric | Value |
|--------|-------|
| Path | `D:\IIC_NEW\iic-booking-backend-rt-port` |
| Branch | `feature/forward-port-reverse-tunnel` |
| Detached HEAD | No |
| Commits on HEAD | ~159 |
| vs `origin/master` | Tip aligned historically; **Phase 2.5 content not committed** |
| Dirty porcelain | ~166 |
| Untracked (sample) | Phase 2.5 docs, SAT migrations/services |
| Generated artifacts in tree | `__pycache__` (ignored); `tmp_commission_run.py` staged risk |
| `.gitignore` | Present (Cookiecutter/Django) |
| CI | `.github/workflows` including deploy on `master` |
| Docs | Strong (`docs/`, `Documentation/`) |
| **Health score** | **42 / 100** |

**Deductions:** Massive dirty tree (−25), release content only local (−20), feature branch tip equals master without RC commits (−8), tmp script hygiene (−5).

---

## 2. Portal Frontend (`iic-booking-frontend`)

| Metric | Value |
|--------|-------|
| Branch | `main` (tracks `origin/master`) |
| Detached HEAD | No |
| Commits | ~181 |
| Dirty | ~13 |
| Untracked | 4 pages (Deployment Center, Lab Infra, Test Dashboard, RDP diagnostics) |
| `.gitignore` | Present (Vite/Node) |
| CI | `.github/workflows` present |
| Structure | `src/`, compose Docker — not `/src` monorepo layout |
| **Health score** | **48 / 100** |

**Deductions:** Critical UI untracked (−30), unstaged wiring (−15), otherwise small/clean tip (+).

---

## 3. Department Sync Agent (`DepartmentSyncAgent`)

| Metric | Value |
|--------|-------|
| Branch | **Detached HEAD** @ `54f1966` |
| Commits reachable | **1** (“Prepare repository for initial release”) |
| Dirty | ~1752 |
| Untracked | ~431 (incl. ~383 `artifacts/dsa-installer/**`) |
| `.gitignore` | Present; **gap:** top-level `artifacts/` vs `Backend/artifacts/` |
| CI | `.github/workflows/integration-tests.yml` |
| Scripts | `Publish-DsaInstaller.ps1`, `publish.ps1` |
| **Health score** | **28 / 100** |

**Deductions:** Detached HEAD (−20), near-empty history vs huge delta (−25), artifact pollution (−15), not on `main`/`develop` tip for release (−12).

---

## 4. Equipment PC Configuration Wizard

| Metric | Value |
|--------|-------|
| Repository | **Not standalone** — `Backend/src/EquipmentPcConfigurationWizard` inside DSA |
| Detached / dirty | Inherits DSA state |
| Project file | `.csproj` present in WT |
| CI | None dedicated |
| Publish | Via portal `publish_equipment_wizard` + local build |
| Docs | Plug-and-play docs in portal backend |
| **Health score** | **35 / 100** *(as a releasable component)* |

**Recommendation:** Treat as **component of DSA repo** for RC1; optional later extract to own repo after DSA recovery.

---

## 5. Remote Analysis Agent (`RemoteAnalysis.Agent`)

| Metric | Value |
|--------|-------|
| Branch | HEAD / incomplete history |
| Commits | **0** (no releasable SHA) |
| Dirty / untracked | ~78 (entire product) |
| `.gitignore` | Present (`bin/`, `obj/`, `data/*.db*`) |
| CI | **Missing** (no `.github`) |
| Publish script | **Missing** (unlike DSA) |
| Local DB files | `data/RemoteAnalysis.db*` risk if ignore fails |
| **Health score** | **18 / 100** |

**Deductions:** No history (−40), no CI (−15), no publish automation (−15), local state files (−12).

---

## Summary table

| Component | Score | Primary blocker |
|-----------|------:|-----------------|
| Portal Backend | 42 | Uncommitted Phase 2.5 |
| Portal Frontend | 48 | Untracked Phase 2 UI |
| DSA | 28 | Detached HEAD + artifacts + history gap |
| Equipment Wizard | 35 | Nested in unhealthy DSA |
| RAA | 18 | No commits / no CI |

**Fleet average (unweighted):** **34 / 100** — Repository Recovery required before RC1.
