# Booking ↔ Remote Analysis Integration

## Architecture

```
Equipment Booking (authoritative: equipment, bookings, payments, samples, workflow, permissions)
        │
        │ Booking Completed (status unchanged for RA phases)
        ▼
Integration Layer (equipment.remote_analysis_integration)
        │  BookingAnalysisEligibilityService
        │  BookingRemoteAnalysisService
        │  BookingWorkspaceFacade / Timeline / Notifications / Reports / Audit
        ▼
Remote Analysis Platform (reservations, sessions, workspace, ops, collaboration)
        ▼
Remote Analysis Agent → Analysis Workstation
```

Booking status is **never** rewritten for Remote Analysis phases. RA progress is tracked via `analysis_*` fields and RA models.

## Integration flow

1. Equipment has `enable_remote_analysis=True`
2. Booking becomes `COMPLETED` → signal evaluates eligibility
3. If eligible → set `analysis_available` + expiry → create `AnalysisReservation` (idempotent) → ensure workspace → notify user
4. User opens Booking Details → **Remote Analysis** section → Launch Desktop (delegates to SessionOrchestrator)
5. Timeline merges booking + reservation + workspace + session events

## Eligibility rules

Checked by `BookingAnalysisEligibilityService`:

| Check | Source |
|-------|--------|
| Equipment enabled | `enable_remote_analysis` |
| Booking not cancelled/refunded/absent | Booking status |
| Experiment completed | `analysis_requires_experiment_completion` + status rule |
| Payment not pending | not `PENDING_PAYMENT` |
| Sample accepted (optional) | Sample trace SAMPLE_ACCEPTED/COMPLETED |
| Not expired | `analysis_expiry` |
| Session limit | `analysis_session_limit` vs `analysis_session_count` |

## Lifecycle mapping

Booking status stays COMPLETED. Parallel RA track:

`REMOTE ANALYSIS AVAILABLE → ACTIVE (session) → COMPLETED/ARCHIVED`

## APIs

| Method | Path |
|--------|------|
| GET | `/api/v1/bookings/{id}/analysis/` |
| POST | `/api/v1/bookings/{id}/analysis/create/` |
| POST | `/api/v1/bookings/{id}/analysis/launch/` |
| GET | `/api/v1/bookings/{id}/analysis/files/` |
| POST | `/api/v1/bookings/{id}/analysis/archive/` |
| GET | `/api/v1/bookings/analysis/dashboard/?scope=user\|faculty\|lab` |

Legacy aliases under `/api/bookings/...` also registered.

## Frontend

- Equipment Form → **Remote Analysis** section (shown when enabled)
- Booking Detail → **Remote Analysis** panel (eligibility, reservation, launch, timeline)
- Deep-link to existing `/remote-analysis` UI (no duplicate Collaboration UI)

## Testing matrix

| Case | Expect |
|------|--------|
| RA disabled on equipment | Not eligible |
| Booking COMPLETED + enabled | Eligible + auto reservation |
| Cancelled booking | Blocked |
| Owner launch | Session created |
| Non-owner launch | 403 |
| Create twice | Idempotent same reservation |
| Existing booking workflow | Unaffected when RA disabled |

## Migration

`equipment.0178_booking_remote_analysis_integration`
