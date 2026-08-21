# Portal Migration — Identity Mapping

## Confirmed (live Channel-I, 2026-08-20)

| Kind | Channel-I field | Meaning |
|------|-----------------|---------|
| Staff | `username` | Employee ID (operator-confirmed) |
| Student | `student.enrolmentNumber` | Student / enrolment ID |
| All | `userId` | Channel-I subject → `internal_id` / `channel_i_user_id` |
| All | `username` | Also stored as `channel_i_username` (never auto emp_id when claim empty) |

## Env

```
CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM=
# after operator approval, set:
CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM=operator_confirmed_map
```

Empty = store Channel-I ids only; do not write `User.emp_id` from Channel-I.

## Conflicts

Existing non-empty `emp_id` ≠ verified candidate → keep existing, status CONFLICT. Never overwrite.
