# Repository Health — After Phase 2.7 Cleanup

**Recalculated:** 2026-07-28  
**Rubric:** same as Phase 2.6 (`01-Repository-Health-Reports.md`) — releasable hygiene, not product quality.

## Before → After

| Component | Before | After | Δ | Why it moved |
|-----------|-------:|------:|--:|--------------|
| Portal Backend | 42 | **44** | +2 | Snapshots + documented cleanup; dirty tip unchanged |
| Portal Frontend | 48 | **50** | +2 | `dist/` cleaned then verified rebuild; Phase 2 UI still untracked |
| DSA | 28 | **46** | +18 | Detached HEAD recovered; `artifacts/` gitignored; bin/obj cleaned (then rebuild) |
| Equipment Wizard | 35 | **48** | +13 | Builds clean; inherits DSA branch recovery |
| RAA | 18 | **28** | +10 | Local DB + bin/obj cleaned; still **0 commits** / no CI |

**Fleet average:** 34 → **43 / 100** (+9).

## Post-cleanup signals (measured)

| Repo | Branch | Detached | Commits | Dirty lines | Untracked | Bin/obj dirs | `artifacts/` |
|------|--------|----------|--------:|------------:|----------:|-------------:|:------------:|
| Backend | `feature/forward-port-reverse-tunnel` | No | 159 | ~155 | ~97 | regenerable | No |
| Frontend | `main` | No | 181 | ~13 | 4 | 0 (+ `dist` after build) | No |
| DSA | `recovery/dsa-phase-2.7` | **No** | 1 | ~1341 | ~48* | regenerable after build | **Yes (disk)** |
| RAA | empty / `HEAD` | N/A | **0** | — | ~75 | regenerable after build | No |

\* Untracked count dropped vs Phase 2.6 largely because `artifacts/` is now ignored (still on disk until confirmed delete).

## Remaining score caps (until commit phase)

| Blocker | Affects |
|---------|---------|
| Phase 2.5 content uncommitted | Backend, Frontend |
| No RAA history | RAA |
| DSA still 1 commit vs huge delta | DSA / Wizard |
| Pending artifact deletes | DSA hygiene |
| No commits / staging (by design in 2.7) | All |

**Verdict:** Hygiene improved; **not yet RC1-commit ready** until an approved commit wave lands features into history.
