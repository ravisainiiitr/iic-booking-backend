# PI Charge Profile

## Overview

`ChargeProfile.pricing_profile` supports:

| Code | Meaning |
|------|---------|
| `standard` | Normal charge profile |
| `discounted` | Existing discounted (often zero) profile |
| `pi` | **PI Charge Profile** — facility rate for assigned PIs |

Normal and PI profiles are edited independently in the Equipment admin UI.

Discounted profiles remain auto-managed as before (zero charges seeded from standard rows).

## Persistence

Confirmed bookings store `Booking.charge_profile` (FK snapshot). Later changes to PI assignment or PI rates do **not** rewrite historical booking charges.
