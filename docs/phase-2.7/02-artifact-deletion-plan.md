# Phase 2.7 — Artifact deletion plan

## A. Deleted in this execution (regenerable build outputs only)

| Repo | Paths | Reason |
|------|-------|--------|
| DSA | All `bin/` and `obj/` under Backend, Shared, tests, data probes, Wizard | MSBuild outputs; already gitignored |
| RAA | `src/RemoteAnalysis.Agent/bin`, `obj` | Same |
| RAA | `data/RemoteAnalysis.db*` | Local SQLite runtime; gitignored pattern |
| Frontend | `dist/` if present | Vite build output; gitignored |

**No source `.cs` / `.tsx` / `.py` deleted.**

## B. PENDING YOUR CONFIRMATION (not deleted)

| Repo | Path | Notes |
|------|------|-------|
| DSA | `artifacts/` (~1715 files: installers, DLLs, zips, probe publishes) | Regenerable via Publish scripts; large; **confirm delete** |
| Backend | `tmp_commission_run.py` | Ad-hoc script currently staged — confirm **delete** vs **keep untracked for later** |

Reply e.g. `confirm delete artifacts and tmp_commission_run.py` to proceed.
