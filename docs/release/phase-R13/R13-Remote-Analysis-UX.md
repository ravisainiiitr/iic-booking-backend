# R13 — Remote Analysis UX (data-first)

## Flow

1. **Open Analysis Workspace**
2. **What data would you like to analyze?** (Current / Previous / Upload) — does **not** wait for RAA allocation
3. Confirm selection
4. Allocation / queue status continues in parallel with accurate offline messaging
5. Prepare → Remote Desktop → End Session → Analyzed Data

## Booking Details

- Primary CTA defaults to **Open Analysis Workspace** (replaces “Analyze Data” as the start action).
- **Analyzed Data** appears when analyzed files exist (download), separate from **Raw Data**.

## Status

| Item | Status |
|------|--------|
| Accurate offline messaging | Implemented (backend experience) |
| Data source gate UI | Implemented (frontend AnalysisWorkspace) |
| CTA rename | Implemented |
| Analyzed Data button visibility | Improved (no longer requires analysisEnded only) |
| Session-wide filesystem capture (“save anywhere”) | **NOT TESTED / PARTIAL** — messaging updated; full RAA watcher deferred |
| Full E2E on FE-SEM with LabVIEW online PC | **BLOCKED** until ops brings LabVIEW PC online or remaps software |
