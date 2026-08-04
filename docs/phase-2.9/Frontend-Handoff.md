# Frontend Handoff - Phase 2.9

## Backend APIs required by Frontend

- Remote Analysis APIs under `/api/v1/analysis/*` for dashboards, reservations, sessions, workspaces, reports, and alerts.
- Booking-bound lifecycle APIs under `/api/v1/bookings/<booking_id>/analysis/*` for Start/Release/Extend/End/Upload flows.
- Lab testing APIs under `/api/v1/lab/testing/*` for SAT dashboard, run execution, result updates, defects, and readiness panels.
- Deployment view endpoints under `/api/v1/deployment/center/` and release listing endpoints when deployment status is surfaced in UI.

## New endpoints from B1-B8

- Reverse tunnel/session execution surfaces in `/api/v1/analysis/session/*` and `/api/v1/analysis/updates/*`.
- Reservation and scheduler flows in `/api/v1/analysis/reservations/*`, `/availability/`, `/queue/`.
- Lab SAT surfaces in `/api/v1/lab/testing/*`.
- Deployment center endpoints in `/api/v1/deployment/*`.

## Authentication requirements

- Portal user endpoints: token/session authenticated, role-gated (Main Admin/Lab In-Charge/Department Admin where applicable).
- Agent-facing endpoints are not for frontend use except read-only dashboards exposed through portal-protected endpoints.
- Health probes (`/api/v1/analysis/health/*`) are unauthenticated and should not be used for user-data UI.

## Expected payloads

- Reservation/session/workspace payloads are JSON objects containing status enums, identifiers, timestamps, and workflow flags.
- Lifecycle actions (`start`, `extend`, `end`, `upload`) return transition state plus operation outcome metadata.
- SAT payloads include run/test/result/evidence/defect objects keyed by UUID and test IDs.

## Configuration dependencies

- Remote analysis defaults come from equipment fields added in B2 (session duration, extension, RAW/RESULTS directories, check-in policy).
- Deployment download links are ticket-based and may require tokenized follow-up requests.
- Feature rendering should tolerate partial availability where environment validations are deferred to Docker/CI.

## Feature flags

- No explicit frontend feature flags were introduced in B1-B8.
- Feature exposure is controlled primarily by endpoint authorization and subsystem readiness.

## Outstanding assumptions

- Frontend should treat remote-analysis status enums as source-of-truth and avoid hard-coded legacy enum sets.
- Frontend must align with final auth-class deployment configuration in CI/staging before production cutover.
- Any UX flows requiring DSA/RAA live behavior remain integration-dependent until those repositories complete their own closure work.

