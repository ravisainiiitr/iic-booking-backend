# Platform RC1 Freeze Certificate

**Freeze date/time:** 2026-08-04 19:02 +0530  
**Certificate type:** Local repository freeze (tags not pushed)

---

## Platform

| Field | Value |
|---|---|
| Platform Name | Institute Instrumentation Centre — Equipment Booking & Remote Analysis Platform |
| Platform Version | `2.5.0-rc1` |
| Qualification | **CONDITIONAL GO** |

---

## Repository Freeze Table

### Backend

| Field | Value |
|---|---|
| Freeze Commit (docs tip) | `c512199d61aac10a1155e7667dbb083d797fc481` |
| Product Baseline | `4ed823579474a9b4d15ca35703543dfc42491184` (B8) |
| Tag | `v2.5.0-rc1` |

### Frontend

| Field | Value |
|---|---|
| Product Tip | `e548c7962af84c611543b03e723ea76683e49476` (F4) |
| Tag | `v2.5.0-rc1` |

### Department Sync Agent

| Field | Value |
|---|---|
| Product Tip | `495e27b56377b1168328189ad82f2bfeee2be826` (D4) |
| Tag | `v1.0.0-rc1` |

### Remote Analysis Agent

| Field | Value |
|---|---|
| Product Tip | `170d689e7e543f73e6b328ae6566ddddc57c0b1e` (R4) |
| Tag | `v1.0.0-rc1` |
| Legacy VERSION file | `1.0.0-RT-RC1` |

---

## Compatibility Summary

| Pair | Result |
|---|---|
| Backend ↔ Frontend | **PASS** |
| Backend ↔ DSA | **PASS** |
| Backend ↔ RAA | **PASS** |
| Frontend ↔ DSA | **PASS** (portal-mediated) |
| Frontend ↔ RAA | **PASS** (portal-mediated) |
| DSA ↔ RAA | **PASS** (portal-mediated) |

---

## Outstanding RC1 Items

1. Frontend package version still `0.0.0`
2. RAA VERSION file still `1.0.0-RT-RC1` (tag is `v1.0.0-rc1`)
3. Installer publish hashes to be regenerated before Deployment Center upload
4. Live commissioning still pending
5. Local tags not pushed to remotes (by policy)

---

## Overall Qualification

**CONDITIONAL GO**

**Reason:**
- Repository freeze complete across all four repositories
- Build verification complete for Backend (prior), Frontend, DSA, and RAA
- Cross-repository compatibility verified at freeze tips
- Deployment and live commissioning remain outstanding

---

## Confirmations

- No source code modified during final freeze
- No commits created during final freeze
- No push / merge / deploy performed
- DSA and RAA local tags created and verified on 2026-08-04
