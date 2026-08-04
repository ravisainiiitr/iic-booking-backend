# Artifact Inventory — Confirmation Gate (Phase 2.7 Step 2)

**Rule:** Do not delete source. Confirm before removing installer packages or ad-hoc scripts.

## Safe to remove (regenerable; already gitignored)

| Repo | Path pattern | Status |
|------|--------------|--------|
| DSA | `**/bin/`, `**/obj/` | **Removed** (30 directory trees) |
| RAA | `**/bin/`, `**/obj/` | **Removed** |
| RAA | `src/RemoteAnalysis.Agent/data/RemoteAnalysis.db*` | **Removed** |
| Frontend | `dist/` | **Removed** (if present) |

## Needs confirmation before remove

| Repo | Path | Why confirm |
|------|------|-------------|
| DSA | `artifacts/` (~1715 files: Setup zips, payloads, DLLs) | Large local publish output; regenerable via `Publish-DsaInstaller.ps1` but may be your only offline copy |
| Backend | `tmp_commission_run.py` | May be useful local tooling; should **not** be in commits either way |

## Already gitignored (no action)

- `node_modules/`, `.vs/`, `.idea/`, `logs/`, `publish/` (DSA)
- Python `__pycache__/`, `.venv/` (backend)

Reply with confirmation to delete the pending items, or say keep them locally.
