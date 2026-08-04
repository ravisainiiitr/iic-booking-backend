# Release Governance — Phase 2.6

## Branch strategy

| Branch | Purpose |
|--------|---------|
| `master` / `main` | Production-deployable tip only |
| `develop` (agents) | Integration for DSA/RAA |
| `release/phase-2.5` | RC stabilization line (create **only when approved**) |
| `hotfix/*` | Prod emergency from tagged GA |

**Never** commit directly to `master` from a dirty laptop WT.

## Release candidate process

1. Repository Recovery complete (this phase’s blockers cleared).  
2. Approved commits on `release/phase-2.5`.  
3. Tag `*-rcN`.  
4. CI builds → fill Release Manifest.  
5. Staging deploy + migration + rollback drill.  
6. Lab SAT → Readiness GO / Conditional GO.  
7. Approval → merge to `master` → prod deploy.  

## Hotfix / patch

- Branch `hotfix/x.y.z` from prod tag.  
- Minimal commits; bump **PATCH**.  
- Same Manifest + shortened SAT smoke.  

## Version tagging

Per [`docs/release/phase-2.5-rc1/03-Versioning-Strategy.md`](../release/phase-2.5-rc1/03-Versioning-Strategy.md).

## Rollback policy

Per [`docs/release/phase-2.5-rc1/05-Rollback-Plan.md`](../release/phase-2.5-rc1/05-Rollback-Plan.md). Prefer image+DB restore over risky migration reverse.

## Approval checklist (before merge to master)

- [ ] Release Manifest complete (no TBD for SHAs/hashes)  
- [ ] CI green on tag  
- [ ] Staging SAT evidence  
- [ ] Critical=0, High=0 or signed waivers  
- [ ] Rollback drill done  
- [ ] Written GO from Lab owner + Engineering lead  

## Current governance state

**Frozen:** no commits/branches until Recovery Report accepted and a later explicit “begin commits” order.
