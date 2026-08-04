# Phase 2.8 - Commit B2 Proposed File Set

**Commit:** B2 - Remote Analysis Session Lifecycle  
**Status:** Proposal only (no staging, no commit)

---

## 1) Scope applied for B2

### Include only
- Session lifecycle/state machine behavior in booking-facing Remote Analysis flow
- End Analysis
- Extend Analysis
- Upload Past Data
- PREPARE workflow continuation
- COLLECT workflow state update
- Session cleanup
- Timeout handling already coupled to cleanup paths
- S3/workspace upload orchestration
- Workstation release
- Booking status transitions related to Remote Analysis

### Exclude
- Equipment configuration and equipment migrations
- RAW/RESULTS equipment directory schema/config
- Software catalog/mapping/resolution
- Availability/reservation allocation engine
- Queue management and waiting logic
- Deployment Center, Fleet, Maintenance, SAT, Plug-and-Play
- Check-in specific flows (`start/release/checkin window`)

---

## 2) Proposed B2 file list

### Include as full files
- `iic_booking/remote_analysis/guacamole/cleanup.py`
  - Session cleanup hardening, workstation release safeguards, reservation completion safeguards.

### Include as partial files
- `iic_booking/remote_analysis/services/commands.py`
  - **Include:** PREPARE `transaction.on_commit` continuation (`mark_prepared` + `retry_prepare`) and existing COLLECT/SYNC completion state updates.
  - **Exclude:** nothing new outside B2 in this diff (keep B1 JOIN_TUNNEL history untouched).

- `iic_booking/equipment/remote_analysis_integration/service.py`
  - **Include:**
    - `end_analysis(...)`
    - `extend_analysis(...)` (lifecycle extension path)
    - `upload_past_data(...)` (upload + sync command orchestration)
  - **Exclude:**
    - Software requirement injection in `ensure_reservation(...)` (`required_software_names`, preferred workstation hints) -> B4.
    - Check-in workflow (`AWAITING_CHECKIN` branches, `release_checkin(...)`, `start_checked_in_session(...)`) -> out of B2.
    - Any lifecycle launch gating tied to explicit check-in transitions.
    - Queue-drain side effect inside `end_analysis(...)` (`process_queue`) -> B4.
    - Queue fairness gate inside `extend_analysis(...)` (`ReservationQueue WAITING` check) -> B4.

- `iic_booking/equipment/remote_analysis_integration/views.py`
  - **Include:**
    - `booking_analysis_end`
    - `booking_analysis_extend`
    - `booking_analysis_files_upload`
  - **Exclude:**
    - `booking_analysis_start` (check-in start)
    - `booking_analysis_release` (check-in release)

- `config/api_router.py`
  - **Include only endpoints for:**
    - `analysis/files/upload`
    - `analysis/end`
    - `analysis/extend`
    - both `/api/v1/...` and legacy `/api/...` aliases
  - **Exclude:**
    - `analysis/start`
    - `analysis/release`
    - `v1/deployment/`
    - `v1/lab/`

### Exclude from B2 (belongs B3/B4/other)
- `iic_booking/equipment/models.py`
- `iic_booking/equipment/serializers.py`
- `iic_booking/equipment/migrations/0182_equipment_analysis_session_duration.py`
- `iic_booking/equipment/migrations/0183_equipment_analysis_raw_results_directories.py`
- `iic_booking/equipment/migrations/0184_equipment_analysis_checkin_policy.py`
- `iic_booking/equipment/remote_analysis_integration/software.py`
- `iic_booking/remote_analysis/services/availability.py`
- `iic_booking/remote_analysis/services/scheduler.py`
- `iic_booking/remote_analysis/guacamole/authorization.py`
- `iic_booking/remote_analysis/tasks.py`
- `iic_booking/remote_analysis/services/checkin.py`
- `config/settings/base.py`
- `iic_booking/remote_analysis/tests/test_end_analysis_and_software_alloc.py` (mixed B2/B3/B4 test file; needs split later)

---

## 3) Self-contained validation

**Result:** B2 is self-contained only after partial-hunk carve-out in mixed files.

Why:
- Raw working-tree diffs in `service.py`, `views.py`, and `api_router.py` include out-of-scope features (check-in, software-aware allocation, deployment/lab routes).
- After excluding those hunks, B2 keeps complete request->service->lifecycle paths for `end`, `extend`, and `files/upload`.
- No B2 migration is required when equipment/config changes are deferred to B3.

---

## 4) Dependency check (B2 vs B3/B4)

### Independence from B3 (equipment integration)
- B2 does not require equipment schema changes if `extend_analysis(...)` keeps fallback-only behavior and no new equipment fields are staged.
- `upload_past_data(...)` uses existing workspace/sync services; it does not require RAW/RESULTS equipment directory fields.

### Independence from B4 (software-aware allocation)
- B2 must exclude software hard-filter injection (`required_software_names`) from reservation creation.
- B2 must exclude queue/waiting logic changes from `end_analysis(...)` and `extend_analysis(...)`.
- With those exclusions, B2 does not require availability/reservation engine changes.

### Ordering
- B2 can be applied immediately after B1.
- B3 then adds equipment schema/config.
- B4 then adds software-aware allocation and queue behavior.

---

## 5) Dependency/risk notes for staging phase

- `service.py` is the primary mixed file; requires deliberate blob/hunk isolation (same method used in B1).
- `api_router.py` must avoid accidentally staging `v1/deployment` and `v1/lab` lines.
- `views.py` should stage only three endpoints (`end`, `extend`, `files/upload`) and skip check-in endpoints.
- Test coverage file is currently mixed (`test_end_analysis_and_software_alloc.py`); B2 should either split tests or defer this file.

---

## 6) Stop point

No files staged for B2 in this step.  
No commit created.
