# Portal Migration Architecture

**Status:** Implemented on feature branch (identity + legacy ledger + cutover state).  
**Production cutover:** NOT executed automatically.

## Layers

```
Channel-I userinfo (live flat + nested)
        ↓
Channel-I Identity Facts (ChannelIIdentityProfile + history)
        ↓
Normalized classification (degree table → UG/PG/Research/UNKNOWN)
        ↓
Department mapping (ChannelIDepartmentMapping → portal Department)
        ↓
Local roles / affiliations / HoD
        ↓
Eligibility (booking, wallet credit, HoD join, lifecycle)
        ↓
Legacy wallet sync (immutable ledger) → final opening balance
```

## Live Channel-I fields (verified 2026-08-20)

| Fact | Live path |
|------|-----------|
| Student detection | non-empty `student` object |
| Degree | `student["branch degree name"]` (also nested `student.branch.degree.name`) |
| Department | `student["branch department name"]` |
| Start | `student.startDate` / `start_date` |
| End | `student.endDate` / `end_date` (null → start+5y) |
| Sex → Gender | `biologicalInformation.sex` → read-only `User.gender` |
| Channel-I ids | `userId`, `username` stored separately |
| Employee ID | only when `CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM` set (`operator_confirmed_map` / `username` / `student.enrolmentNumber`) |

## Migration phases (DB)

`PREPARATION` → `PARALLEL_OPERATION` → `FINANCIAL_FREEZE` → `FINAL_SYNC` → `RECONCILIATION` → `NEW_PORTAL_ACTIVE` → `OLD_PORTAL_READ_ONLY` → `OLD_PORTAL_REDIRECT` → `ARCHIVED`  
Plus `MIGRATION_INTERRUPTED` for resumable failure.

Doc aliases: LEGACY_ACTIVE/MIGRATION_PREPARATION→PREPARATION, MIGRATION_READY→PARALLEL_OPERATION, MIGRATION_RUNNING→FINAL_SYNC, MIGRATION_VERIFICATION→RECONCILIATION, LEGACY_REDIRECT→OLD_PORTAL_REDIRECT.

## Safety

- Legacy MySQL: read-only
- No silent user merge by email/name/mobile
- Wallet key: verified Employee ID only
- Booking gate: `PortalMigrationState.end_user_booking_enabled` enforced in booking APIs
- Final migration requires admin confirmation (MIGRATE) — not auto-run by agents
