# WhatsApp notifications (Meta WhatsApp Business Cloud API)

> Templates are now managed from the catalogue in code: see **Template management** at the end of this page. Template
> names and wording in the sections below are history; `iic_booking/whatsapp/template_catalogue.py` is what counts.

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
- Sending fails closed: while `WHATSAPP_PILOT_NUMBERS` is set, only those numbers receive messages; with no list, nobody does unless `WHATSAPP_ALLOW_ALL` is on. Checked when queued and again when sent.
- Every consent change is kept in `WhatsAppConsentEvent` with its time and source.
- Meta events are handled once (`WhatsAppWebhookEvent`, no content or number stored). Incoming messages from people with an opt-in record are kept in `WhatsAppInbound`, readable only by the Main Administrator (`/api/whatsapp/inbound/`). Both are deleted after `WHATSAPP_RETENTION_DAYS` (default 90) by the daily `whatsapp.purge_expired` task; consent history is kept.
- An incoming message keeps two times: `meta_timestamp` (when Meta says it was sent, in UTC; empty if Meta's value is missing or malformed, and on rows stored before this field existed) and `received_at` (when this server stored it). The message is stored either way.
- A user who replies **STOP** is opted out (START opts them back in); opting out before a queued message is sent
  cancels it.
- Delivery status (sent → delivered → read, or failed with Meta's reason) arrives on the webhook, which only accepts
  requests signed with the app secret.
- Network errors and Meta 5xx / 429 are retried with backoff (Celery task `whatsapp.deliver`); a refusal (e.g. a
  template that is not approved) is recorded as failed and not retried.
- `GET /api/whatsapp/status/` (Main Administrator): whether it is configured and on, opted-in users, messages by status.

## Template management

`iic_booking/whatsapp/template_catalogue.py` is the approved list: 15 templates, each with its name, wording, values, examples, recipient and trigger. It sends nothing; these commands only talk to Meta's template API.

| Command | What it does |
|---|---|
| `manage.py whatsapp_sync_templates --dry-run` | The default. Lists exactly what would be submitted. Changes nothing, here or at Meta. Add `--show-wording` for the text |
| `manage.py whatsapp_sync_templates --status` | Stores Meta's status, category, language and rejection reason, then prints the report |
| `manage.py whatsapp_sync_templates --submit --batch-size 5` | Submits missing templates, one batch. Run it again for the next batch |
| `manage.py whatsapp_sync_templates --correct <name>` | One edit of one rejected template, after its wording changed in the catalogue |
| `manage.py whatsapp_template_report [--refresh]` | Approved, pending, rejected, paused or disabled, missing, requiring correction, and not wired |

**Settings:** `WHATSAPP_WABA_ID` has no default. Every command first checks that this account's name at Meta equals `WHATSAPP_WABA_EXPECTED_NAME`, so templates cannot go to the test account by mistake. The token is the existing `WHATSAPP_ACCESS_TOKEN`.

**Rules:**
- A name and language already at Meta is never submitted again, whatever its status. Meta refuses it anyway (error 100, subcode 2388024).
- A rejected template is never resubmitted automatically. `--correct` needs someone to ask, a changed wording, and fewer than 3 earlier corrections. It edits the existing template, because Meta refuses a new one with that name.
- A rate limit, token, permission, network or server error stops the batch. Meta allows 100 new templates an hour per account.
- Every action goes in `WhatsAppTemplateAudit`, with no token, ID or number.
- The daily task `whatsapp.sync_template_statuses` refreshes statuses (GET only).

**Sending:** `notify` and `deliver` both require an `APPROVED` row for the configured account. Anything else is recorded as skipped. Opt-in, the pilot list and `WHATSAPP_ENABLED` still apply first. Approved templates belong to their account: the sender must be in the same WABA as `WHATSAPP_WABA_ID` before a message can succeed.

**Not in the catalogue, because the portal has no such step:** a reschedule request or refusal, since reschedules happen at once. Also not covered: wallet recharges, payments on external checkouts before the booking exists, and internal wallet refunds as "refund completed".
