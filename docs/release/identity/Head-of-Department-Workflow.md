# Head of Department Workflow

HoD is assigned only by Main Administrator (`HeadOfDepartmentAssignment`).
One active HoD per internal department.

Undergraduate students:

- May join **normal faculty** (existing wallet join).
- May join **only their mapped department’s active HoD**.
- Cannot join another department’s HoD → `403 HOD_DEPARTMENT_MISMATCH`.

Other students: `403 HOD_NOT_AVAILABLE_FOR_USER_TYPE`.
Unmapped department: `403 STUDENT_DEPARTMENT_UNRESOLVED`.
Unresolved classification: `403 USER_TYPE_UNRESOLVED`.

Enforced in `POST /api/wallet/join-request/` when `HOD_AFFILIATION_ENABLED`.
HoD is not assumed to be the wallet owner unless they have a wallet and the student joins it.
