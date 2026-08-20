# Wallet Credit Facility — Final Qualification

## OLD AUTOMATIC CREDIT

| Item | Status |
|------|--------|
| FOUND | Recharge temporary credit (`WalletCreditFacilitySettings` / `try_activate_…`); department faculty overdraft floors (`department_faculty_credit_floor` / avail) |
| REMOVED/DISABLED | Activation no-op; debit floors forced to `0.00`; faculty avail API `410` + service raises retired; recharge `credit_facility_opted_in` forced false |
| HISTORICAL DATA PRESERVED | Yes — no rewrite of past SubWallet transactions or historical facility rows |

## NEW CREDIT FACILITY

| Item | Status |
|------|--------|
| IMPLEMENTED | Models, services, APIs, admin + user UI, invoice PDF, Celery overdue/reminders, reconcile command, migration `0098` |
| FEATURE FLAG | `WALLET_CREDIT_FACILITY_V2_ENABLED` default **false** + `WalletCreditPolicy.enabled` default **false** |

## ELIGIBILITY

| Type | Behaviour |
|------|-----------|
| STUDENT | 403 `CREDIT_NOT_ALLOWED_FOR_USER_TYPE` |
| FACULTY / STAFF (eligible set) | May request when feature on |
| UNKNOWN | 403 `USER_TYPE_UNKNOWN` |

## CHANNEL-I PROFILE

Uses existing portal fields; snapshot at submit/approve. Unavailable → **Not available**. Source labelled Channel-I/Portal. Date of Joining from `User.joining_date` when present (not invented).

## ADMIN APPROVAL

Request immutable; approve / reduce / reject / clarification; audit events immutable; APPROVED ≠ CREDITED until ledger post.

## FINANCIAL LEDGER

Credit via `SubWallet.credit`; repayment via `SubWallet.debit`; outstanding from credit ledger; reconcile read-only.

## INVOICE

Issued on credit post; PDF via ReportLab; payments/receipts tracked; paid only after settlement ledger, not merely positive wallet balance.

## SECURITY

RBAC enforced; IDOR returns 404; concurrency via `select_for_update`; audit immutability tested.

## TESTS

PostgreSQL pytest: **15 passed** (see Test Report). Frontend build not claimed as production-ready qualification.

## PRODUCTION

| Item | Status |
|------|--------|
| WRITES MADE | None to production financial data in this task |
| FEATURE ENABLED | **false** (default) |
| MIGRATIONS APPLIED | Local migration file created; **do not** apply to production automatically |

## FINAL STATUS

**PASS — WALLET CREDIT FACILITY IMPLEMENTED AND QUALIFIED FOR CONTROLLED STAGING/PILOT**

Not production-ready merely because code builds. Enable only after staging migration + pilot smoke with flag and policy switches.
