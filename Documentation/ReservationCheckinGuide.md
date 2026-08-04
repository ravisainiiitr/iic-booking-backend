# Reservation Check-in Guide

Two-stage allocation maximizes Analysis PC utilization.

## Flow

1. Scheduler allocates a compatible PC → status **AWAITING_CHECKIN**
2. User is notified: “Your Analysis Environment is ready.”
3. Countdown (default **10 minutes**, per equipment `analysis_checkin_minutes`)
4. User clicks **Start Analysis Session** → Guacamole + JOIN_TUNNEL + prepare + launch
5. Or **Release Reservation** → PC freed, queue advances

If the timer expires, configurable policy applies:

| Policy | Behaviour |
|--------|-----------|
| `END_OF_QUEUE` (default) | Re-queue at end |
| `RETRY_LATER` | Re-queue for next allocation |
| `CANCEL_AFTER_N` | Cancel after N missed check-ins |

## APIs

| Method | Path |
|--------|------|
| POST | `/api/v1/bookings/{id}/analysis/` (analyze) — may return `awaiting_checkin` |
| POST | `/api/v1/bookings/{id}/analysis/start/` — explicit start |
| POST | `/api/v1/bookings/{id}/analysis/release/` — release without starting |

Experience payload includes `checkin.remaining_seconds` for the countdown UI.

## Windows shared-PC lockdown (administrators)

Remote Analysis users must not shut down shared PCs. Apply via GPO / Local Security Policy on Analysis PCs (lab admins retain full control):

- User Rights: remove Shutdown / Restart for standard analysis accounts
- Hide Shut Down / Restart / Sleep / Hibernate from Start menu (User Configuration → Administrative Templates → Start Menu and Taskbar)
- Optional: disable Lock screen via policy when using Guacamole NLA (or keep lock disabled for shared console accounts)
- Do **not** apply these restrictions to Domain Admins / local Administrators

Document the applied GPO name in the CIF runbook.
