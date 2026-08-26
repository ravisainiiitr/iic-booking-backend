# Copilot V2 Phase B — Controlled E2E Report

**Date:** 2026-08-26  
**Backend tag:** `v2.5.43.4-copilot-v2-phase-b-e2e`  
**Method:** Test-account-only gate (`COPILOT_BOOKING_E2E_TEST_MODE` + allowlisted `is_test_account`)  
**Global mutation flags during/after test:** OFF  

## Verdict

**READY FOR CONTROLLED PRODUCTION ENABLEMENT**

Global booking mutation flags remain **OFF** until Main Administrator explicitly enables them.  
Wallet mutation flags remain **OFF** (Phase C).

## Test account

| Field | Value |
|------|--------|
| User ID | 78 |
| Email | `test.faculty@iic-booking.test` |
| Type | faculty |
| `is_test_account` | true |
| Notes | Dedicated seeded QA account. `test.individual_student` (77) was waitlisted by domain policy on probed equipment — not used for execute path. |

Password not recorded in this report.

## Equipment / slots

| Field | Value |
|------|--------|
| Equipment | Electron Paramagnetic Resonance (EPR) (`equipment_id=43`) |
| Original slot | `26037` — 2026-08-31 04:00–04:30 UTC |
| Reschedule slot | `26038` — 2026-08-31 04:30–05:00 UTC |

## Booking outcome

| Field | Value |
|------|--------|
| Booking ID (pk) | 457 |
| Virtual ID | `IICEPR202600005` |
| Status after create | `BOOKED` |
| Status after reschedule | `BOOKED` (slot 26038) |
| Final status after cancel | `REFUNDED` (portal cancel+refund path) |

## Wallet

| Field | Value |
|------|--------|
| Before | ₹103,616.99 |
| Copilot estimate | ₹40.00 |
| Domain `total_charge` / wallet applied | ₹0.00 / ₹0.00 |
| After create | ₹103,616.99 (unchanged — domain did not debit for this faculty/EPR configuration) |
| After cancel | ₹103,616.99 |

**Note:** Copilot wallet mutations stayed OFF. No manual balance changes. Domain booking service applied ₹0 for this booking; estimate ≠ debit in this configuration (reported explicitly).

## Confirmation / security / idempotency

| Check | Result |
|------|--------|
| Soft phrases (`okay` / `looks good` / `maybe`) | Do **not** map to confirm intent |
| Create without confirm | No booking |
| Confirm via `POST /mutations/confirm/` (FE button path) | Success |
| Wrong confirmation token | Rejected |
| Foreign user execute | Rejected (`*_DISABLED` / not allowlisted) |
| Foreign cancel prepare | Rejected |
| Unavailable reschedule target | Rejected |
| Idempotent create replay | One booking |
| Idempotent cancel replay | One cancel |
| Audit `tool_executed` | Present for prepare/execute create/reschedule/cancel |

## Frontend E2E

Automated browser click-through was **not** run in this qualification window.  
Create confirmation was exercised through the **same authenticated HTTP endpoint** the production Confirm Booking button calls (`/api/v1/research-copilot/mutations/confirm/`). Proposal cards remain on FE build `c470efb`.

## Bugs found and fixed during qualification

1. **No account-scoped flag** — added `COPILOT_BOOKING_E2E_TEST_MODE` + `COPILOT_BOOKING_TEST_USER_IDS` (fail-closed; requires `is_test_account`).
2. **Domain bridge JSON parse** — `Request` without `JSONParser` → `UnsupportedMediaType`.
3. **Cancel/reschedule bridge** — `@api_view` requires Django `HttpRequest`, not pre-wrapped DRF `Request`.
4. **`Equipment.id` in estimate tools** — AttributeError; switched to `eq.pk`.
5. **E2E slot selection** — must be outside `reschedule_hours_threshold` or user cancel fails.

Probe booking `455` (early PXRD inside threshold) cleaned via existing admin `perform_booking_cancellation` (not SQL delete).

## Tests executed

- Django: Phase A + Phase B suites — **35 OK** after fixes
- Controlled live: prepare → confirm create → verify → reschedule → verify → cancel → verify — **PASS**

## Final feature-flag state (production)

```
COPILOT_BOOKING_CREATE=false
COPILOT_BOOKING_CANCEL=false
COPILOT_BOOKING_RESCHEDULE=false
COPILOT_BOOKING_E2E_TEST_MODE=false
COPILOT_BOOKING_TEST_USER_IDS=
COPILOT_WALLET_RECHARGE=false
COPILOT_WALLET_CREDIT=false
```

## Enablement guidance (Main Administrator)

Only after review of this report, enable **global** booking flags one environment at a time:

```
COPILOT_BOOKING_CREATE=true
COPILOT_BOOKING_CANCEL=true
COPILOT_BOOKING_RESCHEDULE=true
```

Keep all `COPILOT_WALLET_*` false until Phase C.

## Safety

- Production T0 migration: **NO**
- Wallet recharge/credit: **NO**
- Global booking mutations during qualification: **NO** (test-account gate only; now OFF)
