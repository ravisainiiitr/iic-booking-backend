# Channel-I Identity Architecture

Channel-I identity facts, portal user classification, internal departments,
HoD assignments, and feature eligibility are **separate layers**.

```
Channel-I identity facts
        ↓
Normalized classification (not Django user_type)
        ↓
Department mapping (Channel-I string → internal Department FK)
        ↓
Portal roles (HoD assignment, admin types)
        ↓
Faculty / HoD affiliations
        ↓
Feature eligibility (booking, wallet credit, join, lifecycle)
```

- `ChannelIIdentityProfile` is source data. History is append-only.
- `User.user_type` remains the portal login role (student/faculty/admin/…).
- `HEAD_OF_DEPARTMENT` is a **local** `HeadOfDepartmentAssignment`, not a Channel-I degree.
- Internal `Department` is portal-owned. Mapping never auto-creates departments when `DEPARTMENT_MAPPING_ENABLED`.
- Nested Channel-I fields parsed: `student.branch.degree.name`, `student.branch.department.name`, `student.start_date`, `student.end_date` (plus existing flat keys).

Feature flags (default **false**):

- `DEPARTMENT_MAPPING_ENABLED`
- `HOD_AFFILIATION_ENABLED`
- `STUDENT_LIFECYCLE_ENABLED`
- `WALLET_CREDIT_ENABLED` / `WALLET_CREDIT_FACILITY_V2_ENABLED`
