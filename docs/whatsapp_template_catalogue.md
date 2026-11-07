# WhatsApp template catalogue

Generated from `iic_booking/whatsapp/template_catalogue.py`, which is what the code submits. All are language `en`, category `UTILITY`. The first three already exist at Meta with this wording.

## iic_booking_confirmed

- **Django event:** `booking_confirmed`
- **Trigger:** Booking created as Booked, or moved from Pending or Awaiting payment to Booked
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Booking Confirmation
- **Footer:** (none)
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
  - `{{5}}` time, for example: 10:00 AM
- **Body:**

```
Dear {{1}},

Your equipment booking has been confirmed.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}
Time: {{5}}

Please visit the IIT Roorkee Equipment Booking Portal for booking details.

IIT Roorkee
```

## iic_booking_cancelled

- **Django event:** `booking_cancelled`
- **Trigger:** Booking event Cancelled or Refunded
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Booking Cancellation
- **Footer:** (none)
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
- **Body:**

```
Dear {{1}},

Your equipment booking has been cancelled.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}

For further information, please visit the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iic_booking_completed

- **Django event:** `booking_completed`
- **Trigger:** Booking event Completed
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Booking Completed
- **Footer:** (none)
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` completion date, for example: 10 Nov 2026
- **Body:**

```
Dear {{1}},

Your equipment booking has been successfully completed.

Booking ID: {{2}}
Equipment: {{3}}
Completion Date: {{4}}

Thank you for using the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_booking_request_received

- **Django event:** `booking_request_received`
- **Trigger:** Booking created with status Pending
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Booking Request Received
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
- **Body:**

```
Dear {{1}},

We have received your equipment booking request. It is waiting for review.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}

You can follow its status on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_booking_rejected

- **Django event:** `booking_rejected`
- **Trigger:** Laboratory rejects a 3D printing or laser cutting job
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Booking Not Accepted
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
- **Body:**

```
Dear {{1}},

Your equipment booking could not be accepted by the laboratory.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}

The reason and the next steps are shown on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_reschedule_requested

- **Django event:** `reschedule_requested`
- **Trigger:** None: the portal has no such step, so nothing sends it
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Reschedule Request Received
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
- **Body:**

```
Dear {{1}},

We have received your request to reschedule this equipment booking.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}

You can follow its status on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_reschedule_approved

- **Django event:** `reschedule_approved`
- **Trigger:** Booking event Rescheduled
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Booking Rescheduled
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
  - `{{5}}` time, for example: 10:00 AM
- **Body:**

```
Dear {{1}},

Your equipment booking has been rescheduled. The new slot is shown below.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}
Time: {{5}}

Please check the updated details on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_reschedule_rejected

- **Django event:** `reschedule_rejected`
- **Trigger:** None: the portal has no such step, so nothing sends it
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Reschedule Request Declined
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
- **Body:**

```
Dear {{1}},

Your request to reschedule this equipment booking could not be accepted. Your original slot stays as booked.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}

The details are shown on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_booking_reminder

- **Django event:** `booking_reminder`
- **Trigger:** Daily booking reminder task
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Booking Reminder
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
  - `{{5}}` time, for example: 10:00 AM
- **Body:**

```
Dear {{1}},

This is a reminder of your equipment booking.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}
Time: {{5}}

The booking details are on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_payment_initiated

- **Django event:** `payment_initiated`
- **Trigger:** Online payment order created for a booking
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Payment Started
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` amount, for example: Rs. 1,500.00
- **Body:**

```
Dear {{1}},

A payment has been started for your equipment booking.

Booking ID: {{2}}
Equipment: {{3}}
Amount: {{4}}

The payment status is shown on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_payment_successful

- **Django event:** `payment_successful`
- **Trigger:** Online payment order marked Paid
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Payment Received
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` amount, for example: Rs. 1,500.00
- **Body:**

```
Dear {{1}},

We have received the payment for your equipment booking.

Booking ID: {{2}}
Equipment: {{3}}
Amount: {{4}}

The receipt details are on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_payment_failed

- **Django event:** `payment_failed`
- **Trigger:** Online payment order marked Failed
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Payment Not Completed
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` amount, for example: Rs. 1,500.00
- **Body:**

```
Dear {{1}},

The payment for your equipment booking was not completed.

Booking ID: {{2}}
Equipment: {{3}}
Amount: {{4}}

You can check the booking and try again on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_payment_pending

- **Django event:** `payment_pending`
- **Trigger:** Booking created with status Awaiting payment
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Payment Due
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` amount, for example: Rs. 1,500.00
- **Body:**

```
Dear {{1}},

Your equipment booking is waiting for payment.

Booking ID: {{2}}
Equipment: {{3}}
Amount: {{4}}

Please complete the payment on the IIT Roorkee Equipment Booking Portal to keep this booking.

IIT Roorkee
```

## iitr_refund_initiated

- **Django event:** `refund_initiated`
- **Trigger:** Bank refund queued, or gateway refund created
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Refund Started
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` amount, for example: Rs. 1,500.00
- **Body:**

```
Dear {{1}},

A refund has been started for your equipment booking.

Booking ID: {{2}}
Refund amount: {{3}}

You can follow it on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_refund_completed

- **Django event:** `refund_completed`
- **Trigger:** Wallet refund made, bank refund closed, or gateway refund processed
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Refund Completed
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` amount, for example: Rs. 1,500.00
- **Body:**

```
Dear {{1}},

The refund for your equipment booking has been completed.

Booking ID: {{2}}
Refund amount: {{3}}

The details are shown on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_waitlist_slot_available

- **Django event:** `waitlist_slot_available`
- **Trigger:** Freed slot within 48 hours, same step as the email
- **Recipient:** Each active waitlist user for that equipment
- **Language / category:** en / UTILITY
- **Header:** Waitlist Slot Available
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` equipment, for example: X-Ray Diffractometer
  - `{{3}}` date, for example: 10 Nov 2026
  - `{{4}}` time, for example: 10:00 AM
- **Body:**

```
Dear {{1}},

A slot has become available for equipment you are waitlisted for.

Equipment: {{2}}
Date: {{3}}
Time: {{4}}

Slots are not held. To book it, please visit the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```

## iitr_sample_received

- **Django event:** `sample_received`
- **Trigger:** Sample lifecycle moves to Sample Accepted
- **Recipient:** The user who made the booking
- **Language / category:** en / UTILITY
- **Header:** Sample Received
- **Footer:** Reply STOP to stop WhatsApp messages.
- **Placeholders:**
  - `{{1}}` user name, for example: Asha Verma
  - `{{2}}` booking ID, for example: XRD202600123
  - `{{3}}` equipment, for example: X-Ray Diffractometer
  - `{{4}}` date, for example: 10 Nov 2026
- **Body:**

```
Dear {{1}},

The laboratory has received and accepted your sample.

Booking ID: {{2}}
Equipment: {{3}}
Date: {{4}}

You can follow the analysis on the IIT Roorkee Equipment Booking Portal.

IIT Roorkee
```
