# I-STEM end-to-end demonstration (split screen)

The portal sends each event to I-STEM within seconds, except during the peak booking window. The production portal
currently talks to **I-STEM staging**, so everything below runs on production with test accounts and shows up on
I-STEM staging.

## Screen layout

| Left half | Right half |
|---|---|
| Portal (equip.iitr.ac.in), signed in as the **Main Administrator**, page **Dashboard → I-STEM Live** (`/admin/istem-live`) | I-STEM staging front end, signed in as the IIT Roorkee institute administrator |

On Windows, click one window and press **Win + ←**, then pick the other window for the right half (or **Win + →**).
Open a second portal window (or a private window) for the **test user** whose bookings you make. Switch to it with
Alt + Tab, or keep it on top of the left half.

I-STEM Live refreshes every 2 seconds:
- **On the way to I-STEM** shows events that are queued and waiting to be sent.
- **Last 24 hours** shows each call once I-STEM has answered. New rows are highlighted. Each row gives the FBR number,
  request ID and confirmation ID to look up on the I-STEM side, and the time from the portal event to I-STEM's answer.

Do not run the demo during the peak booking window (slot opening). I-STEM calls are held then, and the page says so.

## Before the demo

1. The test external user's profile is complete (billing name, address, GSTIN or "not registered").
2. The instrument is mapped and verified under **I-STEM Equipment Mapping** (XRF, id 44, is mapped to I-STEM 12468).
3. Have one test external user who is **new** to I-STEM, and one whose email or mobile **already exists** on I-STEM
   staging.

## Steps and what appears

| # | Portal action (left) | I-STEM Live row | Where to see it on I-STEM (right) |
|---|---|---|---|
| 1 | A new external user finishes registration | I-STEM user created | Users: the new user |
| 2 | An external user already on I-STEM registers | Existing I-STEM user linked | Users: the same user, no duplicate created |
| 3 | The user books and pays for an XRF slot | Booking created on I-STEM (FBR), then Slot allocated (booking confirmed) | Bookings / FBR: the FBR number, status Confirmed, the slot allocated |
| 4 | The user moves the booking to another slot | Rescheduled: booking created for the new slot, then Rescheduled: old slot cancelled on I-STEM | The new slot is confirmed and the old one is cancelled |
| 5 | The lab operator marks the booking Completed | FBR executed (booking completed) | FBR: Executed, with the amount |
| 6 | The user cancels a second booking (full refund) | Booking cancelled on I-STEM | The booking is Cancelled |
| 6b | A cancellation that keeps a fee | Cancelled: FBR executed for the fee kept | FBR executed for the retained amount |
| 7 | Main Admin: I-STEM Equipment Mapping → an unmapped instrument → **Create on I-STEM** | Equipment created on I-STEM | Equipment: the new instrument and its I-STEM equipment ID |

I-STEM usually answers each call in a few seconds. Staging can be slower: a token alone has taken about 12 s. A refused
or uncertain call is shown in red with I-STEM's message, and is never sent twice by itself.

## Notes

- I-STEM accepts a reschedule only while a booking is pending. A confirmed booking is moved by creating and confirming
  the new slot, then cancelling the old one (I-STEM's instruction, 13 Nov 2026). That is why step 4 shows two rows.
- When the new slot overlaps the old one, the old booking is cancelled first, because I-STEM refuses an overlap.
- The scheduled sync every few minutes stays on as a safety net. It never sends anything twice.
