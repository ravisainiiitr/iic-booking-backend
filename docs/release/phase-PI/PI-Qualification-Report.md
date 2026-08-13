# PI Qualification Report

**Date:** 2026-08-13  
**Release tag:** `v2.5.38-r12-pi-pricing`  
**Backend PR:** [#79](https://github.com/ravisainiiitr/iic-booking-backend/pull/79) (MERGED)  
**Frontend PR:** [#14](https://github.com/ravisainiiitr/iic-booking-frontend/pull/14) (MERGED)  
**Migration:** `0187_equipment_pi_and_pi_charge_profile`

## Verdicts

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Equipment supports multiple Faculty PIs | **PARTIAL** | Model + serializer + admin/UI; live admin E2E NOT TESTED |
| Admin can add/remove PI | **PARTIAL** | EquipmentForm + Django admin; production UI NOT TESTED |
| PI Charge Profile exists | **PASS** (code) | `ChargeProfilePricingProfile.PI = "pi"` + migration 0187 |
| Normal Charge Profile independent | **PASS** (code) | Sync excludes PI when updating standard rows |
| Current User PI → PI pricing | **PASS** (unit) | `test_pi_pricing.py` |
| Wallet Owner PI → PI pricing | **PASS** (unit) | `test_wallet_owner_pi_counts` |
| Neither PI → Normal pricing | **PASS** (unit) | `test_standard_when_not_pi` |
| PI but no PI profiles → fallback | **PASS** (unit) | `test_fallback_when_pi_but_no_pi_profiles` |
| Backend determines PI status | **PASS** (code) | `pi_pricing.resolve_pricing_profile_for_user` |
| Frontend cannot spoof PI status | **PASS** (unit/code) | `test_frontend_cannot_spoof_via_resolver` |
| Historical bookings retain price | **PASS** (architecture) | `Booking.charge_profile` FK snapshot |
| Historical PI change after booking | **NOT TESTED** | No controlled DB E2E this cycle |
| Cancellation/refund of PI booking | **NOT TESTED** | Relies on existing charge_profile snapshot path |
| Unauthorized cannot change PI | **NOT TESTED** | Relies on existing admin equipment permissions |
| Live booking quote E2E | **NOT TESTED** | No authorized production test booking |

**Overall Equipment PI Assignment + Pricing:** **PARTIAL**

## Security review

- Booking/estimate paths call `_get_charge_profile_pricing_profile_for_user` → `resolve_pricing_profile_for_user`.
- Resolver uses current user + wallet owner + `EquipmentPI` rows + active PI `ChargeProfile`s.
- Client-supplied `is_pi` / spoofed amounts are not used for profile selection.

## Migration notes

- Depends on `equipment.0186_booking_analysis_closed_at`.
- Creates `EquipmentPI`, `EquipmentPIAuditLog`; extends `pricing_profile` choices with `pi`.
- Index names aligned to migration (`equipment_e_equipme_pi_act_idx`, `equipment_e_faculty_pi_act_idx`).
- Rollback of 0187: **not verified** — do not claim reverse migrate is safe without a restore drill.
