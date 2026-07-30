# Analyze Data & Analysis Workflows — Operator & User Guide

## Research journey (user-facing terminology)

```
Book Equipment → Perform Experiment → Analyze Data → Download Processed Results
```

| UI term | Meaning |
|---------|---------|
| **Analyze Data** | Portal button on a completed booking |
| **Analysis Workspace** | Page for workflow selection, progress, launch |
| **Analysis Session** | Active interactive analysis session |
| **Analysis Environment** | Software environment (e.g. OriginPro Environment) — not a PC name |
| **Processed Results** | Final outputs available for download |

Backend APIs and database may still use `remote_analysis` names for compatibility. Do **not** expose Guacamole, Remote Desktop, or workstation hostnames to researchers.

## What researchers see

1. Open a completed booking.
2. Click **Analyze Data** or **Open Analysis Workspace**.
3. Review **workflow** name, estimated duration, steps, and required software.
4. Click **Launch Analysis** — an Analysis Environment is allocated automatically.
5. Complete each step; progress and checkpoints are shown.
6. When finished, **Processed Results Available** — download from Results / analysis files.

## Analysis Workflows

An **Analysis Workflow** is a reusable multi-step pipeline (template).  
An **Analysis Job** is the runtime instance for one booking.

Example:

```
PXRD Standard Analysis
  → Step 1 HighScore Plus
  → Step 2 PDF Database Search
  → Step 3 OriginPro
```

Workspace folders:

`RawData/` · `Step01/` · `Step02/` · … · `FinalOutput/` · `Scratch/` · `Logs/` · `Processed/` (compat) · `Metadata/analysis.json`

- Step N outputs go to `StepNN/`.
- Step N+1 uses the previous step folder as input.
- Finalization copies into `FinalOutput/` and `Processed/`.

### Input requirements

Workflows may require RAW, calibration, and/or reference files (verified before launch when configured).

### Capability tags

Software packages advertise capabilities (Peak Fitting, Image Analysis, …). Steps may reference a **capability** instead of a hard-coded package so software can be swapped later.

### Output verification

If a step declares expected outputs (e.g. `*.xy`) and they are missing, the job enters **Needs Operator Review** instead of advancing blindly. Staff/owners can force-complete when appropriate.

### Resume anywhere

If the session fails mid-workflow, **Resume** continues at the first incomplete step — never restarts from step 1.

### Same environment vs handoff

The existing scheduler is reused (no second scheduler):

- If one Analysis Environment has **all mandatory** software → keep the user there.
- Otherwise allocate the next environment for the next step and hand off folders seamlessly.

Scoring includes software match, GPU, RAM/load (via heartbeat), department affinity, historical health, and multi-software coverage.

## Operator setup

1. Django Admin → **Analysis software catalog** (+ optional **capabilities**).
2. Prefer **Analysis workflows** + **Equipment analysis workflows** (default mapping).
3. Legacy **Equipment analysis software** still works; migration `0014` auto-creates single-step workflows from those rows.
4. Optional **Equipment analysis pool** — preferred environments for that instrument.
5. **Remote analysis settings** — CTA label, require RAW, stage-on-launch, prefer workflow.
6. Portal **Workflow Designer** at `/admin/analysis-workflows` (staff).

## APIs

| Method | Path |
|--------|------|
| GET | `/api/v1/bookings/<id>/analysis/` — workflows, job, can_analyze, terminology fields |
| GET | `/api/v1/bookings/<id>/analysis/workflows/` |
| POST | `/api/v1/bookings/<id>/analysis/analyze/` — `workflow_id` and/or legacy `mapping_id` |
| GET | `/api/v1/bookings/<id>/analysis/job/` |
| POST | `/api/v1/bookings/<id>/analysis/job/steps/<n>/complete/` |
| POST | `/api/v1/bookings/<id>/analysis/job/pause/` · `.../resume/` |
| CRUD | `/api/v1/analysis/workflows/` (staff designer) |
| GET | `/api/v1/analysis/workflows/ops/` — running jobs, success rate |

## Migrations

1. `0013_analyze_data_catalog_and_settings` — catalog + legacy profile backfill.  
2. `0014_analysis_workflows` — workflows/jobs + **automatic** Equipment→Software → single-step workflow backfill.

```bash
python manage.py migrate remote_analysis
```

## Portal routes

- `/analysis-workspace/:bookingId` — researcher Analysis Workspace  
- `/admin/analysis-workflows` — Workflow Designer + ops strip  

## Security notes

- Only booking owners launch / complete / pause / resume.
- Workstation identity is omitted from researcher API payloads.
- Audit events: `WorkflowStarted`, `StepCompleted`, `WorkflowHandoff`, `WorkflowCompleted`, `WorkflowPaused`, `WorkflowResumed`.
- Collaboration roles (Owner/Collaborator/Viewer/Observer) and AI assistance fields are reserved for later versions.

## Scalability

- Folder handoffs copy within the workspace volume; large trees may later move to Celery.
- Published workflow versions are immutable; edits create drafts then publish.
- Allocation remains the single scheduler path used by Analyze Data.
