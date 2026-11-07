# WhatsApp template catalogue

Generated from `iic_booking/whatsapp/template_catalogue.py`, which is what the code submits. All are language `en`. All are category `UTILITY` except `iitr_verification_code`, which is `AUTHENTICATION`. The first three already exist at Meta with this wording.

## iic_booking_confirmed

- **Django event:** `booking_confirmed`
- **Trigger:** Booking created as Booked, or moved to Booked from Pending or Awaiting payment, including by online or offline payment
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

## iitr_booking_rescheduled

- **Django event:** `booking_rescheduled`
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

The payment details are on the IIT Roorkee Equipment Booking Portal.

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

## iitr_verification_code

- **Django event:** `otp_code`
- **Trigger:** Proving a WhatsApp number, signing in with a proved number, or an I-STEM OTP the person asked to get on WhatsApp
- **Recipient:** The person who asked for the code
- **Language / category:** en / AUTHENTICATION
- **Header:** (none)
- **Footer:** This code expires in 10 minutes.
- **Button:** Copy code
- **Placeholders:**
  - `{{1}}` one-time code, for example: 482913
- **Body:** Meta writes this wording itself; the template only switches on the security line and the expiry. The portal cannot add its own name to it.

```
{{1}} is your verification code. For your security, do not share this code.
```
