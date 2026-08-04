# RC1 Readiness Report — IIC Laboratory Platform 2.5.0-rc1

**Role:** Release Manager assessment  
**Date:** 2026-08-04  
**Scope:** Release engineering readiness to *begin* creating Git history and an RC build — **not** production GO  

---

## Verdict

| Decision | Value |
|----------|-------|
| **RC1 engineering readiness** | **NO GO** to produce a shippable RC artifact **today** |
| **Why** | B1 is committed and B2 (Remote Analysis execution engine) is in controlled commit creation; remaining backend/frontend/DSA/RAA work is still uncommitted and untagged |
| **Next gate** | After approved commits on `release/phase-2.5` + clean CI builds → re-score to **Conditional GO** for staging Lab SAT |
| **Production deploy** | **NO GO** (SAT gate still closed; Manifest empty) |

---

## Repository health

| Repo | Health | Notes |
|------|--------|-------|
| Portal Backend | **Poor for RC** | Dirty WT; tip == master without Phase 2.5 |
| Portal Frontend | **Poor for RC** | Features untracked/unstaged only |
| DSA | **Poor for RC** | Detached HEAD; artifacts pollution risk; huge uncommitted delta |
| RAA | **Critical for RC** | Zero commits |

### Controlled commit progress (Phase 2.8)

| Commit | SHA | Status |
|--------|-----|--------|
| B1 — Reverse Tunnel restoration | `d4d50e29891bce543d6d9258958fb744df71d90e` | Accepted |
| B2 — Remote Analysis execution engine | `500629b60992839fce99be2d2257230dfcb43ba3` | Accepted |
| B3 — Deployment Center | `24fb089613ad7fd51dd39bde24ebf1f2845a385d` | Accepted |
| B4 — Plug-and-Play Platform | `TBD (assigned after commit)` | In progress |

---

## Release health

| Area | Status |
|------|--------|
| Release Manifest | Template only — all SHAs TBD |
| Build reproducibility | Documented; not achievable for 2.5 from remote tip |
| Dependency matrix | Draft proposed versions |
| Versioning policy | Defined (2.5.0-rc1 / agents 1.0.0-rc1) |
| Rollback plan | Drafted |
| Installer automation | DSA script strong; RAA weak; signing TBD |
| Documentation pack | This folder + phase-2.5 audits |
| Traceability | **Blocked** until tags exist |

---

## Outstanding risks

1. Accidental `git add` of DSA `artifacts/` (~383 binaries).  
2. Migrating production with RA empty `0015` + new `0017` without staging dump test.  
3. Lab model/SAT migration drift if split incorrectly.  
4. Deploying frontend 2.5 before backend 2.5.  
5. Treating dirty laptop builds as RC.

---

## Technical debt (release-impacting)

| Item | Impact |
|------|--------|
| Uncommitted multi-week backlog | Cannot version or roll back |
| Missing Deployment Center / SAT automated tests | Relies on Lab SAT |
| No RAA publish script | Agent RC incomplete |
| N+1 fleet (H-10) | Scale risk |
| Unsigned installers | Policy waiver needed for RC |

---

## Missing tests / docs / automation

| Missing | Priority |
|---------|----------|
| Git history + tags | P0 |
| CI tag builds for all four repos | P0 |
| RAA publish + SHA pipeline | P0 |
| `artifacts/` gitignore at repo root (DSA) | P0 |
| Deployment Center API tests | P1 |
| SAT API tests | P1 |
| Authenticode in CI | P1 (P0 for GA) |
| Admin guide refresh for Lab/SAT pages | P1 |

---

## Release confidence

| Question | Confidence |
|----------|------------|
| Can we explain what will ship? | **High** (audits + commit plan) |
| Can we rebuild bit-for-bit from git tomorrow? | **None** until commits |
| Can we pass Lab SAT on a closed RC set? | **Unknown** — not yet built |
| Can we roll back? | **Medium** (plan exists; drill not done) |

---

## Recommended sequence (still no action without approval)

1. Approve junk exclusion + commit plan.  
2. Create `release/phase-2.5` **when approved**.  
3. Apply commit groups; tag `*-rc1`.  
4. CI build → fill Manifest.  
5. Staging deploy + migration drill + rollback drill.  
6. Lab SAT → update this report to Conditional GO / GO.  
7. Only then approve merge to `master` / production.

---

## Sign-off

| Role | Decision | Name | Date |
|------|----------|------|------|
| Release Manager | **NO GO** (shippable RC today) | (this report) | 2026-08-04 |
| Product / Lab owner | | | |
| Engineering lead | | | |

**STOP:** No commits, branches, merges, or pushes were performed in producing this report.
