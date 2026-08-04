# Phase 2.8 Blocking Report

## Problem

Safe creation of **Commit B2 (Remote Analysis Session Lifecycle)** is currently blocked at the hunk-carving stage.

## Cause

B2-owned lifecycle logic is interleaved in the same modified files with B3/B4-owned logic:
- `iic_booking/equipment/remote_analysis_integration/service.py`
- `iic_booking/equipment/remote_analysis_integration/views.py`
- `config/api_router.py`

Required B2 exclusions (check-in, software-aware allocation, queue/waiting, deployment/lab routes) are mixed inside adjacent and overlapping code regions.  
Automated non-interactive carving attempt (index-blob generation script) failed due parser/escaping complexity while performing broad regex transformations, which increases the risk of accidental hunk removal or semantic drift.

## Impact

Cannot confidently guarantee all of the following simultaneously in one safe automated step:
- strict B2 architectural boundary
- zero source-code loss risk
- preservation of deferred B3/B4 hunks for later commits
- migration-safe and self-contained B2 commit

Proceeding with coarse staging would violate architectural boundaries.  
Proceeding with aggressive automated rewrite/carve risks unintended code edits.

## Possible solutions

1. **Controlled manual index carving (recommended)**
   - Build B2-only index blobs for each mixed file from HEAD + explicit whitelisted hunks.
   - Verify each staged file with `git diff --cached <file>`.
   - Commit only after line-by-line validation.

2. **Temporary split branch workflow**
   - Create a temporary local branch/worktree for B2 extraction.
   - Commit B2 there, then cherry-pick onto current branch.
   - Continue B3/B4 from original mixed tree.

3. **Refactor-then-commit approach (not preferred)**
   - Physically separate lifecycle/check-in/allocation code into dedicated modules first.
   - Then create commits by module ownership.
   - This introduces extra structural churn and violates "no unrelated refactor" preference.

## Recommended solution

Use **Solution 1**: controlled manual index carving with explicit per-file whitelists and staged diff verification before commit.

## Exact repository state (at block)

- Repository: `D:/IIC_NEW/iic-booking-backend-rt-port`
- Branch: `feature/forward-port-reverse-tunnel`
- Last accepted commit: `d4d50e29891bce543d6d9258958fb744df71d90e` (B1)
- Staging area: no new B2 staging completed
- Working tree: large mixed backend/docs changes remain present

## Current commit completed

- **Completed:** B1 only
- **Not completed:** B2

## Next blocked commit

- **B2 — Remote Analysis Session Lifecycle**

