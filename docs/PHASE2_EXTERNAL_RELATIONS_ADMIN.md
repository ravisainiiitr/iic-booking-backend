# External Relations Administrator & Organization Administrator

## Roles

### Main Administrator (`admin`)
- Full system access, including internal RBAC caps for Department Administrators.

### Department Administrator (`dept_admin`)
- Internal department only.
- Caps granted by Main Admin (`DeptAdminPermissionGrant`).
- May grant subordinate permissions to OIC / Lab / Accounts (`StaffPermissionGrant`).

### OIC / Lab / Accounts (`manager` / `operator` / `finance`)
- Staff roles under a Department Administrator.
- If no explicit staff grants exist yet, role defaults apply (`bookings.manage`, `equipment.manage`, `reports.view`, etc.).
- Once any staff grant row exists for that user, only explicit grants apply.

### External Relations Administrator (`external_relations`)
- Reviews external organization KYC / verification requests.
- Approves or rejects organization verification (same organization-request APIs as Main Admin).
- May manage external user verification workflows from the External User Management UI.

### Organization Administrator (`org_admin`)
- Belongs to an **external** department/organization.
- Manages members of that organization only (`org.users.manage`).
- UI: `/organization/users`.
- Cannot create additional Organization Administrators from that panel.

## Permission codes (selected)

| Code | Who typically uses it |
|------|------------------------|
| `bookings.manage` | OIC, Lab, Dept Admin (when capped) |
| `wallet.manage` | Accounts, Dept Admin (when capped) |
| `reports.view` | Staff with report access |
| `permissions.manage_staff` | Dept Admin |
| `external.org.verify` | External Relations / Main Admin |
| `org.users.manage` | Org Admin |

## Current-user payload

`UserSerializer` includes `rbac_permissions` for the authenticated subject only (avoids N+1 on admin user lists). Frontend helpers live in `src/lib/rbac.ts`.
