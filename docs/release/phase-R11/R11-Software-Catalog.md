# R11 — Software Catalog

Central `AnalysisSoftwareCatalog` is the single source of truth.

- Auto-populated from RAA `InstalledSoftware` inventory (slug-deduped).
- Global disable/archive = not eligible for allocation.
- Per-RAA `allocation_enabled` disables one install without uninstalling.
- SPA: `/remote-analysis/software-catalog`
