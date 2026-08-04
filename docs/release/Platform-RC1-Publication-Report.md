# Platform RC1 Publication Report

**Document type:** Batch 1 — Repository Publication (Atomic)  
**Platform version:** `2.5.0-rc1`  
**Publication window:** 2026-08-04 ~21:48–21:53 IST (+05:30)  
**Final verification timestamp:** 2026-08-04 21:53:17 +05:30  
**Status:** **Platform RC1 Published**

---

## Release order

1. Portal Backend  
2. Frontend  
3. Department Sync Agent  
4. Remote Analysis Agent  

All four completed successfully. No mid-batch abort.

---

## Publication results

| # | Repository | Branch published | Commit (peeled) | Tag | Remote | `ls-remote` tag peel | Result |
|---|---|---|---|---|---|---|---|
| 1 | Portal Backend (`iic-booking-backend`) | `master` fast-forward `52ddcfc..c512199` | `c512199d61aac10a1155e7667dbb083d797fc481` | `v2.5.0-rc1` | `github-ravisainiiitr:ravisainiiitr/iic-booking-backend.git` | `c512199…` | **PASS** |
| 2 | Frontend (`iic-booking-frontend`) | `master` fast-forward `ffa5af4..e548c79` | `e548c7962af84c611543b03e723ea76683e49476` | `v2.5.0-rc1` | `github-ravisainiiitr:ravisainiiitr/iic-booking-frontend.git` | `e548c79…` | **PASS** |
| 3 | Department Sync Agent | `recovery/dsa-phase-2.7` (new remote branch) | `495e27b56377b1168328189ad82f2bfeee2be826` | `v1.0.0-rc1` | `https://github.com/ravisainiiitr/DepartmentSyncAgent.git` | `495e27b…` | **PASS** |
| 4 | Remote Analysis Agent | `release/reverse-tunnel-rc1` `dcb37d5..170d689` | `170d689e7e543f73e6b328ae6566ddddc57c0b1e` | `v1.0.0-rc1` | `https://github.com/ravisainiiitr/RemoteAnalysisAgent.git` | `170d689…` | **PASS** |

### Backend product baseline (unchanged)

| Field | Value |
|---|---|
| Product baseline (B8) | `4ed823579474a9b4d15ca35703543dfc42491184` |
| Docs / tag tip | `c512199d61aac10a1155e7667dbb083d797fc481` |
| Tag annotation | Records both SHAs |

---

## Platform verification table (post-publish)

| Component | Expected tag | Expected commit | Remote peeled commit | Match |
|---|---|---|---|---|
| Backend | `v2.5.0-rc1` | `c512199d61aac10a1155e7667dbb083d797fc481` | `c512199d61aac10a1155e7667dbb083d797fc481` | **PASS** |
| Frontend | `v2.5.0-rc1` | `e548c7962af84c611543b03e723ea76683e49476` | `e548c7962af84c611543b03e723ea76683e49476` | **PASS** |
| DSA | `v1.0.0-rc1` | `495e27b56377b1168328189ad82f2bfeee2be826` | `495e27b56377b1168328189ad82f2bfeee2be826` | **PASS** |
| RAA | `v1.0.0-rc1` | `170d689e7e543f73e6b328ae6566ddddc57c0b1e` | `170d689e7e543f73e6b328ae6566ddddc57c0b1e` | **PASS** |

---

## Final platform consistency matrix

| Pair | Result |
|---|---|
| Backend ↔ Frontend | **PASS** (both `v2.5.0-rc1` published) |
| Backend ↔ DSA | **PASS** |
| Backend ↔ RAA | **PASS** |
| Frontend ↔ DSA | **PASS** (portal-mediated) |
| Frontend ↔ RAA | **PASS** (portal-mediated) |
| DSA ↔ RAA | **PASS** (portal-mediated) |

---

## Warnings (non-blocking for Batch 1)

1. **Annotated tag object SHAs** differ from peeled commits (expected). Always verify with `refs/tags/<tag>^{}`.
2. **DSA** published on branch `recovery/dsa-phase-2.7` (not `main`/`master`). Tag `v1.0.0-rc1` is the immutable release pointer.
3. **RAA** VERSION file remains `1.0.0-RT-RC1` while git tag is `v1.0.0-rc1` — correlation required in Deployment Center later.
4. **Frontend** `package.json` version still `0.0.0` — git tag is authoritative for RC1.
5. Untracked local docs (freeze certificate, release manifest, this report) were **not** pushed as part of Batch 1 (no new commits created during publication).
6. Production server was **not** updated; Batch 1 is GitHub publication only.

---

## Explicit non-actions

- No Docker image build  
- No registry push  
- No database migration  
- No production server changes  
- Batch 2 not started  

---

## Release status

# Platform RC1 Published

Awaiting authorization for **Batch 2 — Image Build**.
