# WhatsApp notifications (Meta WhatsApp Business Cloud API)

Built 3 Nov 2026. **Off by default.** Nothing is sent until the switch and the four Meta values below are set, and then
only to users who opted in under *Profile → WhatsApp notifications*.

## What is sent

| Portal event | Template name (Meta) | Body parameters, in order |
|---|---|---|
| Booking made (and paid) | `iic_booking_confirmed` | {{1}} booking ID, {{2}} equipment, {{3}} first slot (e.g. `Mon 03 Nov 2026, 10:00 AM`) |
| Booking cancelled / refunded | `iic_booking_cancelled` | {{1}} booking ID, {{2}} equipment, {{3}} first slot |
| Booking completed (results available) | `iic_booking_completed` | {{1}} booking ID, {{2}} equipment, {{3}} link to My Bookings |

One message per portal event (no duplicates on retries). Email and in-app notifications are unchanged.

Suggested template text to submit in WhatsApp Manager (category **Utility**, language **English**):

- `iic_booking_confirmed`: "Your IIC booking {{1}} for {{2}} is confirmed for {{3}}. Details are in My Bookings on the IIT Roorkee equipment booking portal. Reply STOP to stop WhatsApp messages."
- `iic_booking_cancelled`: "Your IIC booking {{1}} for {{2}} ({{3}}) has been cancelled. Any refund is credited as shown in My Bookings. Reply STOP to stop WhatsApp messages."
- `iic_booking_completed`: "Your IIC booking {{1}} for {{2}} is complete. Results and documents: {{3}}. Reply STOP to stop WhatsApp messages."

Different names can be used: set `WHATSAPP_TEMPLATES` in settings to a mapping such as
`{"booking_confirmed": "my_template_name"}` (the parameters must stay in the order above).

## Set-up (done by IIT Roorkee in Meta Business Manager)

1. Create or use the institute's **Meta Business account** and verify the business.
2. In **WhatsApp Manager**, add a phone number that is not used by the WhatsApp app (it becomes the sender), and set
   the display name (e.g. "IIC IIT Roorkee").
3. Create a **System user** with access to the WhatsApp account and generate a **permanent access token** with the
   `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
4. Submit the three templates above and wait for approval.
5. In the Meta app's WhatsApp → Configuration, set the webhook:
   - Callback URL: `https://equip.iitr.ac.in/api/whatsapp/webhook/`
   - Verify token: any long random string (the same value goes into `WHATSAPP_VERIFY_TOKEN`)
   - Subscribe to the `messages` field.
6. Put the values into the production environment (GitHub secrets for the *Apply WhatsApp Config* workflow):

| Setting | Where it comes from |
|---|---|
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Manager → phone number → Phone number ID |
| `WHATSAPP_ACCESS_TOKEN` | the system user's permanent token |
| `WHATSAPP_APP_SECRET` | Meta app → Settings → Basic → App secret (verifies webhook signatures) |
| `WHATSAPP_VERIFY_TOKEN` | the string chosen in step 5 |
| `WHATSAPP_ENABLED` | `True` to start sending |
| `WHATSAPP_PILOT_NUMBERS` | optional: comma-separated numbers (`+9198…`) that alone receive messages during a pilot |

## Costs

Meta charges per conversation (utility messages in India are a fraction of a rupee each; the first 1,000 service
conversations a month are free). The portal sends only the three utility templates above, only to opted-in users.

## Behaviour

- Opt-in: `GET/PUT /api/whatsapp/me/` (the profile card). The number defaults to the profile phone, normalised to
  E.164 (ten digits are read as an Indian mobile).
- A user who replies **STOP** is opted out (START opts them back in); opting out before a queued message is sent
  cancels it.
- Delivery status (sent → delivered → read, or failed with Meta's reason) arrives on the webhook, which only accepts
  requests signed with the app secret.
- Network errors and Meta 5xx / 429 are retried with backoff (Celery task `whatsapp.deliver`); a refusal (e.g. a
  template that is not approved) is recorded as failed and not retried.
- `GET /api/whatsapp/status/` (Main Administrator): whether it is configured and on, opted-in users, messages by status.
