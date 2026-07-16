# Phase 2: External Relations Administrator

Phase 1 ships only the internal RBAC hierarchy:

- `admin` remains the single Main Administrator.
- `dept_admin` is added for internal departments only.
- `manager`, `operator`, and `finance` continue as the subordinate internal staff roles.

## Phase 2 boundary

External organization verification is intentionally deferred.

When Phase 2 starts, the system should introduce a single **External Relations Administrator** role with these responsibilities:

- Review external organization KYC details during signup or verification.
- Classify the organization into the correct external type such as Educational Institute, Govt R&D Organizations, Industry, or External Startup/MSME.
- Approve or reject the verification outcome before the external users proceed with normal department-facing booking workflows.

## Explicit non-goals

Phase 2 should **not** introduce:

- a separate Org Admin hierarchy for external organizations,
- per-organization administrator accounts,
- organization-level wallets or nested external membership trees.

External booking requests and operational handling should continue to stay with the institute departments, not with a separate external admin tree.
