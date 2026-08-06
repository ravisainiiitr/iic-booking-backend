# Phase R.6.1 — Complete the Software-Centric Remote Analysis Platform

| Field | Value |
|-------|--------|
| Mode | Gap closure (not redesign) |
| Status | **Implemented** on `feature/r6-remote-analysis-software-centric` |
| Depends on | R6.0 assessment + R6 vertical slice |
| Rule | Reuse catalog/mapping/inventory/scheduler. No parallel models/APIs. Preserve bookings/agents/queues. |

## Index (R6.1 admin & ops docs)

| Doc | Purpose |
|-----|---------|
| [R6.0 Current State Assessment](R6.0-Current-State-Assessment.md) | Baseline (pre–R6.1) |
| [R6.0.1 Updated Assessment (delta)](R6.0.1-Updated-Assessment-Delta.md) | What changed vs R6.0 |
| [R6.1 Software Catalog Administration](R6.1-Software-Catalog-Administration.md) | SPA CRUD + API |
| [R6.2 Equipment Mapping](R6.2-Equipment-Mapping.md) | Dept → Equipment → Software matrix |
| [R6.3 Inventory](R6.3-Inventory.md) | RA inventory admin |
| [R6.4 Discovery Engine](R6.4-Discovery-Engine.md) | Multi-scanner + delta sync |
| [R6.5 Scheduler](R6.5-Scheduler.md) | Allocation order + diagnostic logs |
| [R6.6 License Management](R6.6-License-Management.md) | License types + scheduler hooks |
| [R6.7 Analysis Workspace](R6.7-Analysis-Workspace.md) | Researcher UX (no PC identity) |
| [R6.8 AI Readiness](R6.8-AI-Readiness.md) | Metadata only |
| [R6.9 API Reference](R6.9-API-Reference.md) | Single source of truth |
| [Gap Closure Report](R6.1-Gap-Closure-Report.md) | Priority gaps closed / deferred |
| [Migration Report](R6.1-Migration-Report.md) | `0023` + dual-read notes |
| [Regression Report](R6.1-Regression-Report.md) | Compatibility checks |
| [Production Readiness](R6.1-Production-Readiness-Recommendation.md) | Go / no-go |

## Legacy R6 planning docs (still valid for architecture)

R6.1–R6.9 **filenames above replace the old planning index names** for operator docs. Original planning files remain for history:

- `R6.1-Gap-Analysis.md`, `R6.2-Proposed-Architecture.md`, `R6.3-Software-Discovery.md`, `R6.4-Scheduler.md` (planning), `R6.5-Database.md`, `R6.6-APIs.md`, `R6.7-Administrator-Guide.md`, `R6.8-User-Workflow.md`, `R6.9-AI-Readiness.md` (planning)

Prefer the **R6.1-named administration docs** for day-to-day ops.

## Headline

| Area | R6.1 result |
|------|-------------|
| Catalog SPA CRUD | Shipped (`/remote-analysis/software-catalog`) |
| Equipment↔Software matrix SPA | Shipped (`/remote-analysis/equipment-software`) |
| Inventory admin richness | Improved (search/filter + fleet enrichment) |
| Agent discovery parity | Multi-scanner (registry, Start Menu, PF/PF(x86), portable) |
| Incremental sync | Client delta + server `sync_mode=delta\|full` upsert |
| Scheduler diagnostics | Per-candidate ACCEPT/REJECT logs |
| License types | Extended enum + seat/server hooks |
| Workspace UX | Description, typical usage, file types, AI tags |
| AI recommender | Still deferred (metadata only) |
| Network license server product | Model/hooks only — not a full license server |
