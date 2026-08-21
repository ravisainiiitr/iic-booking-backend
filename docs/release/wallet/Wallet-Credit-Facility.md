# Wallet Credit Facility — Architecture

## Purpose

Replace automatic low-balance / temporary / negative-balance credit with an
**administrator-approved** Wallet Credit Facility.

## Feature flag

| Layer | Control | Default |
|-------|---------|---------|
| Environment | `WALLET_CREDIT_FACILITY_V2_ENABLED` | `false` |
| DB policy | `WalletCreditPolicy.enabled` | `false` |

Both must be true for users to submit requests. **Student restriction is always
enforced** when the feature path is exercised.

## Models

- `WalletCreditPolicy` — global limits (not hardcoded ₹1000)
- `WalletCreditFacility` — request lifecycle; `requested_amount` immutable
- `WalletCreditLedgerEntry` — immutable credit/repayment ledger
- `WalletCreditInvoice` — demand for settlement (not a tax invoice)
- `WalletCreditPayment` — settlement receipts
- `WalletCreditAuditEvent` — immutable audit trail
- `profile_snapshot` JSON on facility — Channel-I/portal fields at submit/approve

## Ledger integration

Credit posting calls `SubWallet.credit(...)` and records a linked
`WalletCreditLedgerEntry` (`WALLET_CREDIT`). Outstanding is derived from the
credit facility ledger (credited − repaid ± adjustments). Wallet balance is
**never** edited outside the existing SubWallet transaction API.

Repayment: `SubWallet.debit(..., minimum_balance_after=0)` + `CREDIT_REPAYMENT`
ledger row + receipt.

## Retired automatic credit

| Mechanism | Status |
|-----------|--------|
| Recharge temporary credit (`try_activate_credit_facility_after_otp_verify`) | No-op; opt-in cleared |
| `subwallet_minimum_balance_after_debit` floors | Always `0.00` |
| Department faculty `avail` API / service | 410 / raises retired |
| Historical recharge / faculty facility rows | Preserved |

## APIs

User:

- `GET /api/wallet/credit-requests/summary/`
- `GET|POST /api/wallet/credit-requests/`
- `GET /api/wallet/credit-requests/<id>/`
- `POST /api/wallet/credit-requests/<id>/repay/`
- `GET /api/wallet/credit-requests/<id>/invoice.pdf`

Admin:

- `GET /api/admin/wallet-credit/`
- `GET /api/admin/wallet-credit/reconcile/` (read-only)
- `GET /api/admin/wallet-credit/<id>/`
- `POST .../approve/`, `reject/`, `clarification/`, `post-credit/`

## Eligibility

- Students (`student`, `individual_student`) → `403 CREDIT_NOT_ALLOWED_FOR_USER_TYPE`
- Empty/unknown `user_type` → `403 USER_TYPE_UNKNOWN`
- One blocking facility at a time (transactional `select_for_update` on user)

## Channel-I profile

Uses existing portal fields (`emp_id`, `internal_id`, `joining_date`, etc.).
Missing values display as **Not available**. No fabricated Date of Joining.
No OAuth tokens exposed.

## Frontend

- User: `/wallet/credit-facility`
- Admin: `/admin/wallet-credit` (+ `/:facilityId` review with Channel-I panel)
