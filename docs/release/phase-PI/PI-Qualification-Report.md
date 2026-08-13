# PI Qualification Report

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Equipment supports multiple Faculty PIs | PARTIAL | Model + serializer + UI implemented; not yet E2E on production |
| Admin can add/remove PI | PARTIAL | Portal EquipmentForm + Django admin inline |
| PI Charge Profile exists | PASS (code) | `ChargeProfilePricingProfile.PI = "pi"` + migration 0187 |
| Normal Charge Profile independent | PASS (code) | Sync excludes PI when updating standard rows |
| Current User PI → PI pricing | PARTIAL | Unit tests mock-covered; DB E2E NOT TESTED |
| Wallet Owner PI → PI pricing | PARTIAL | Unit tests mock-covered; DB E2E NOT TESTED |
| Neither PI → Normal pricing | PARTIAL | Unit tests mock-covered |
| Backend determines PI status | PASS (code) | `pi_pricing.py` |
| Frontend cannot spoof PI status | PASS (code) | Resolver ignores client `is_pi` |
| Historical bookings retain price | PASS (architecture) | Existing `Booking.charge_profile` FK |
| Cancellation/refund | NOT TESTED | Relies on existing charge_profile snapshot |
| Unauthorized cannot change PI | NOT TESTED | Relies on existing admin equipment permissions |
| PI pricing test matrix | PARTIAL | `test_pi_pricing.py` SimpleTestCase only |

**Overall PI Assignment + Pricing:** PARTIAL
