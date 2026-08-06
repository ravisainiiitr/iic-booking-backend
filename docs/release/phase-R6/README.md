# Phase R.6 — Remote Analysis Software-Centric Architecture

| Field | Value |
|-------|--------|
| Mode | Assessment + targeted redesign |
| Status | **Docs complete** · vertical slice implemented on `feature/r6-remote-analysis-software-centric` |
| Depends on | R.2 / R.3 Remote Analysis baseline |
| Rule | **Assess first.** Reuse existing modules. No duplicate catalog/APIs/admin pages. Preserve backward compatibility. |

## Primary home

This folder in **`iic-booking-backend-deploy`** is the primary R6 documentation home (same pattern as phase-R2 / phase-R3).

Cross-repo pointers:

| Repo | Location |
|------|----------|
| Portal backend (primary) | `docs/release/phase-R6/` (this folder) |
| Remote Analysis Agent | `RemoteAnalysisAgent/docs/release/phase-R6/` (agent discovery focus + pointer) |
| Portal frontend | `iic-booking-frontend/docs/release/phase-R6/README.md` (UI pointer) |

## Index

| Doc | Purpose |
|-----|---------|
| [R6.0 Current State Assessment](R6.0-Current-State-Assessment.md) | What exists vs partial vs missing (with file paths) |
| [R6.1 Gap Analysis](R6.1-Gap-Analysis.md) | Desired software-centric model vs reality |
| [R6.2 Proposed Architecture](R6.2-Proposed-Architecture.md) | Target topology and dual-read / reuse plan |
| [R6.3 Software Discovery](R6.3-Software-Discovery.md) | Agent + installer discovery and inventory sync |
| [R6.4 Scheduler](R6.4-Scheduler.md) | Equipment → software → best RA PC |
| [R6.5 Database](R6.5-Database.md) | Tables / migrations (reuse first) |
| [R6.6 APIs](R6.6-APIs.md) | Existing endpoints + R6 deltas |
| [R6.7 Administrator Guide](R6.7-Administrator-Guide.md) | Catalog, mappings, inventory ops |
| [R6.8 User Workflow](R6.8-User-Workflow.md) | Analyze Data without picking a PC |
| [R6.9 AI Readiness](R6.9-AI-Readiness.md) | Metadata only — no recommender |

## Assessment headline

**Most of the software-centric stack already exists** in the portal backend and RA agent. R6 does **not** introduce a second catalog, scheduler, or inventory pipeline.

| Already solid | Needs enhancement | Deferred (documented) |
|---------------|-------------------|------------------------|
| Catalog, equipment↔software, inventory sync, auto-allocate, queue | Workspace software selection → allocation; license_type choices; AI metadata fields | SPA catalog CRUD; deep license server integration; GPU util; AI recommender |

## R6 implementation slice (this branch)

1. Allocation uses **selected** catalog software (not always every mapped app).
2. Analysis Workspace lets researchers **select** equipment-mapped software; still never picks an RA PC.
3. Catalog `license_type` choices + reserved `ai_tags` / `ai_metadata` (migration `0022`).
