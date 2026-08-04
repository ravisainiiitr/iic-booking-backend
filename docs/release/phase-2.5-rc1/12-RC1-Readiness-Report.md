# RC1 Readiness Report — IIC Laboratory Platform 2.5.0-rc1

**Role:** Release Manager assessment  
**Date:** 2026-08-04  
**Scope:** Release engineering readiness to *begin* creating Git history and an RC build — **not** production GO  

---

## Verdict

| Decision | Value |
|----------|-------|
| **RC1 engineering readiness** | **NO GO** to produce a shippable RC artifact **today** |
| **Why** | Backend B1-B8, Frontend F1-F4, DSA D0-D4, and RAA R1-R4 are committed; RC remains blocked only on cross-repo integration/SAT gates and release-environment validation |
| **Next gate** | After approved commits on `release/phase-2.5` + clean CI builds → re-score to **Conditional GO** for staging Lab SAT |
| **Production deploy** | **NO GO** (SAT gate still closed; Manifest empty) |

---

## Repository health

| Repo | Health | Notes |
|------|--------|-------|
| Portal Backend | **Poor for RC** | Dirty WT; tip == master without Phase 2.5 |
| Portal Frontend | **Good for RC history, pending cleanup** | F1-F4 committed; only residual non-functional local modifications remain |
| DSA | **Good for RC history** | D0-D4 committed on `recovery/dsa-phase-2.7`; working tree clean |
| RAA | **Good for RC history** | R1-R4 committed on `release/reverse-tunnel-rc1`; working tree clean |

### Controlled commit progress (Phase 2.8)

| Commit | SHA | Status |
|--------|-----|--------|
| B1 — Reverse Tunnel restoration | `d4d50e29891bce543d6d9258958fb744df71d90e` | Accepted |
| B2 — Remote Analysis execution engine | `500629b60992839fce99be2d2257230dfcb43ba3` | Accepted |
| B3 — Deployment Center | `24fb089613ad7fd51dd39bde24ebf1f2845a385d` | Accepted |
| B4 — Plug-and-Play Platform | `61b151fdb66d5dffef84dbbe9786e05e458ad167` | Accepted |
| B5 — Laboratory Infrastructure | `932d016bb1119e71ada4df4959ab508217d46c52` | Accepted |
| B6 — Diagnostics & Reporting | `49bfd66835e1c9d6d40e84184cf2dab28cd7281d` | Accepted |
| B7 — SAT Dashboard | `7b53a93542950ed30df8a27f235bfe7cfc02693d` | Accepted |
| B8 — Cross-cutting Stabilization | `4ed823579474a9b4d15ca35703543dfc42491184` | Accepted |
| D0 — DSA Repository Recovery baseline | `b657c20228a9c7f273d78c0af6c6b25e059fa1f7` | Accepted |
| D1 — DSA Discovery & Provisioning | `f58f8e5937c4f8e117d1af14b5e9ae01c9757b4e` | Accepted |
| D2 — DSA Configuration Platform | `6c0191f1c7187ce005756264d9aa209c11546213` | Accepted |
| D3 — DSA Monitoring Platform | `6d9e5dd52ac80ceb564d947fba3fe16082e11224` | Accepted |
| D4 — DSA Documentation & Release assets | `495e27b56377b1168328189ad82f2bfeee2be826` | Accepted |
| R1 — RAA Foundation & Enrollment bootstrap | `e841afbf0a693b348c833ead5ce958efa8e06044` | Accepted |
| R2 — RAA Identity/Heartbeat/Reverse Tunnel | `93533bfad6608c0c36d06cf4a90c8ca118deb285` | Accepted |
| R3 — RAA Session execution workspace maintenance | `80314f07f7f4ad24dc5614cc4162e71d9141294f` | Accepted |
| R4 — RAA Documentation & Installer assets | `170d689e7e543f73e6b328ae6566ddddc57c0b1e` | Accepted |

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
