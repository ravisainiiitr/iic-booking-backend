# DSA Commit Readiness

## Current readiness verdict

- **Is D1 feasible now?** No, not safely in current index/worktree state.
- **Is another normalization pass required?** Yes.

## Why D1 is not yet feasible

1. DSA currently has simultaneous staged and unstaged edits across core files that define D1 boundaries.
2. Cross-capability interleaving is heavy in startup/DI/persistence/portal/heartbeat layers.
3. Snapshot + migration artifacts are present in mixed state and cannot be reliably carved by hand.

## Remaining blockers

- `MM`/`AM` mixed files in core foundation and service wiring (notably `Program.cs`, DI extensions, portal client, heartbeat service, persistence context).
- `DsaDbContextModelSnapshot.cs` tied to broad migration surface.
- High artifact noise (large deletion set) competing with source review signal.

## Estimated carve-outs

- Mixed index/worktree files requiring controlled hunk-carve attention: **~24 high-priority files**
- Additional carve-outs likely once D1 baseline is isolated: **30-60**
- Non-carvable pairs to keep atomic (migration + snapshot clusters): **multiple groups**

## Recommendation

Perform a **normalization pass before any D1 commit**:

1. Preserve raw state snapshots (already exported under `docs/phase-3/`).
2. Normalize index/worktree to a single unambiguous staging strategy (content-preserving, no history rewrite).
3. Recompute D1-only file set and carve plan from normalized state.
4. Re-run lightweight build check before first commit attempt.

## Artifacts generated in this pass

- `docs/phase-3/_dsa-status-porcelain.txt`
- `docs/phase-3/_dsa-staged-files.txt`
- `docs/phase-3/_dsa-unstaged-files.txt`
- `docs/phase-3/_dsa-capability-inventory.csv`
- `docs/phase-3/_dsa-capability-summary.txt`
- `docs/phase-3/DSA-Normalization-Plan.md`

## Stop condition

Per instruction, no staging, no commits, no push/merge/rebase/amend were performed in this normalization pass.

