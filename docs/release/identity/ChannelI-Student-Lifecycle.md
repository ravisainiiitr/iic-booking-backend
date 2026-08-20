# Channel-I Student Lifecycle

Semantics (portal local date):

- Student is **active through** `effective_end_date`.
- Student is **disabled after** `effective_end_date` (`localdate() > end_date`).

Validity source:

| Source | When |
|--------|------|
| `CHANNEL_I_END_DATE` | `student.end_date` present (authoritative) |
| `START_DATE_PLUS_5_YEARS` | end missing, start present → start + 5 calendar years |
| `ADMIN_EXTENSION` | local +6 calendar month extension (no Channel-I end date) |
| `UNRESOLVED` | no start and no end — do not guess |

Celery: `users.expire_channel_i_students` (no-op unless `STUDENT_LIFECYCLE_ENABLED`).
Disable reason: `CHANNEL_I_STUDENT_END_DATE`. Idempotent. Users/bookings/wallets are not deleted.

Local extension never overrides a later Channel-I `end_date`.
Reactivation is not automatic on login.
