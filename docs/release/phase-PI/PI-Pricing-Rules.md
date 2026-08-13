# PI Pricing Rules

Authoritative module: `iic_booking/equipment/pi_pricing.py`.

## Decision flow

```
Current User
     ↓
Wallet Owner (via user.get_accessible_wallet().user)
     ↓
EquipmentPI (active)

If current user OR wallet owner is an active Equipment PI
AND at least one active PI ChargeProfile exists for the equipment:
    → pricing_profile = pi
Else:
    → existing standard / discounted resolution
```

## Examples

| Booking user | Wallet owner | Equipment PI | Result |
|--------------|--------------|--------------|--------|
| Faculty A (PI) | Faculty A | Faculty A | PI |
| Student S | Faculty B (PI) | Faculty B | PI |
| Faculty C | Faculty C | Faculty A | Normal |
| Faculty A (PI) | Faculty A | Faculty A | but no PI profiles configured → Normal/Discounted fallback |

## Server authority

Frontend must not submit `is_pi`, spoofed charge amounts, or profile codes that override server resolution. Charge estimate and booking creation always resolve the profile server-side.
