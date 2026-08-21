# Portal Migration — Student Lifecycle

## Rules

1. Student iff non-empty Channel-I `student` object.
2. Degree classification via `StudentDegreeClassification` on degree name; unknown → UNKNOWN (never silent UG).
3. End date: Channel-I `endDate` authoritative when present.
4. Else: `startDate + 5 calendar years`.
5. Else: LIFECYCLE_UNRESOLVED / validity UNRESOLVED — do not invent expiry.
6. Celery `users.expire_channel_i_students` disables (`force_inactive`) after effective end — never deletes.
7. +6 calendar month extension only when expiry derived from start+5y; faculty wallet owner requests; Main Admin approves.
8. If Channel-I later sends `endDate`, it overrides local extension; extensions blocked.

Feature flag: `STUDENT_LIFECYCLE_ENABLED` (default false).
