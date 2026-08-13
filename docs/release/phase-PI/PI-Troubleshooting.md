# PI Troubleshooting

## Quote shows Normal while user expects PI

1. Confirm Faculty is active on `EquipmentPI` for that equipment.
2. Confirm at least one **active** ChargeProfile with `pricing_profile=pi` for the user's `user_type`.
3. If user is not PI but uses a shared wallet, confirm **wallet owner** is an Equipment PI.
4. Frontend `is_pi` flags are display-only — server resolver is authoritative.

## PI assigned but no PI charge rows

Safe fallback: STANDARD / DISCOUNTED via existing discounted-equipment rules (`test_fallback_when_pi_but_no_pi_profiles`).

## Historical booking amount changed after PI edit

Should **not** happen if booking stored `charge_profile` FK at creation. If observed, treat as a financial defect and investigate booking create path — not as expected PI behavior.
