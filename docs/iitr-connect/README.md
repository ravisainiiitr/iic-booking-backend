# IITR Connect

IITR Connect is a private technical collaboration and knowledge platform inside the IIT Roorkee Equipment Booking Portal.

> Connect. Discuss. Share Knowledge. Solve Together.

## At a glance

| | |
|---|---|
| Backend | New app `iic_booking.connect` (Django / DRF). API at `/api/v1/connect/` |
| Frontend | `/connect/*` pages, `src/components/connect/*`, `src/lib/connectApi.ts` (React / Vite) |
| Feature flag | `IITR_CONNECT_ENABLED` (default **False**), optional `IITR_CONNECT_PILOT_EMAILS` |
| Migrations | `connect 0001` to `0006` (additive tables and data only) |
| New infrastructure | None. Uses PostgreSQL full-text search, the existing private S3 bucket (or local storage), Celery and Beat, and the portal's email and in-app notifications |
| Status | Built and tested locally (M1–M13). **Not pushed, merged, deployed or migrated in production.** |

## Documents

| Document | Contents |
|---|---|
| [architecture.md](architecture.md) | Architecture assessment, design decisions, how existing portal pieces are reused |
| [er-diagram.md](er-diagram.md) | Entity relationships and key constraints |
| [api.md](api.md) | All endpoints (generated from the OpenAPI schema) |
| [security-review.md](security-review.md) | Threat model, controls, test evidence, residual risks |
| [performance-review.md](performance-review.md) | Indexes, query counts, pagination, background work |
| [migrations.md](migrations.md) | Each migration and how to apply or roll it back |
| [user-guide.md](user-guide.md) | Guide for members, facility staff and administrators |
| [rollout-plan.md](rollout-plan.md) | Safe production rollout and rollback procedure |
| [local-testing.md](local-testing.md) | How to run the tests and a local demo |
| [files-and-migrations.md](files-and-migrations.md) | Every file added or changed, with its commit |
| [test-results.md](test-results.md) | Test results and regression comparison |
| [known-limitations.md](known-limitations.md) | Known limitations, open decisions, follow-ups |
