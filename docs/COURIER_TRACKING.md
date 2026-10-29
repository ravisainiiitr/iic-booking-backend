# External Courier Tracking and Sample Arrival

External users who send samples by courier register the courier and tracking (AWB) number on their booking. The
portal follows the shipment through TrackCourier.io within a strict monthly quota, tells the user about meaningful
status changes, and tells the lab when the courier reports a delivery. Internal IIT Roorkee users always declare
**manual delivery**: no courier tracking, no provider calls, no quota.

A delivery reported by the courier is **not** laboratory receipt. Physical receipt stays in the existing sample
lifecycle (Held at Office / Forwarded to Lab / Sample Accepted ...), recorded by lab staff.

Status: implemented on branch `feature/courier-tracking` (backend `D:\IIC_NEW\wt-be-courier`, frontend
`D:\IIC_NEW\wt-fe-courier`), both from `origin/master`. Not committed, not deployed, no production migration, no real
tracking request made.

## Architecture and data flow

```
Booking page (SampleArrivalPanel)             Courier tracking page / Main Admin page
        │ POST /bookings/<id>/arrival/                   │ GET /shipments/, /admin/overview/
        ▼                                                 ▼
courier_tracking.services.register_arrival ──► SampleArrival (+ "Sample Sent" trace event for couriers)
        │ on commit: lookup task (INITIAL)                │
        ▼                                                 │
quota.reserve (row lock on TrackingQuotaMonth) ──► TrackCourierClient.track ──► TrackCourier.io GET /v1/track
        │ quota.complete (ledger + provider usage)        │
        ▼                                                 │
services._apply (replace checkpoints, forward-only, ordering guard) ──► notifications (ledger → email / in-app)
        ▲
        │ webhooks.accept (verify, store) → webhooks.process      Celery Beat: scheduled checks, sweep,
  POST /webhooks/trackcourier/ (when a plan includes webhooks)    reminders, courier-list refresh
```

Backend app: `iic_booking/courier_tracking/`. Department enablement reuses `iic_booking/department_modules/`.

| File | Role |
|---|---|
| `constants.py` | Arrival modes, internal status model, provider state maps, documented error codes, lookup outcomes |
| `models.py` | `SampleArrival`, `TrackingLookup`, `TrackingQuotaMonth`, `Courier`, `TrackingWebhookEvent`, `ShipmentNotification`, `ShipmentReminder`, `CourierTrackingAlert`, `CourierTrackingSettings` |
| `eligibility.py` | Internal / external rules, department switch |
| `client.py` | TrackCourier.io client (HTTPS, `X-API-Key`, timeouts, `error.code` classification, safe logging) |
| `parsing.py` | Separate PascalCase (track) and snake_case (webhook) parsers; tracking-number normalisation |
| `quota.py` | Reservation under a row lock, reconciliation with provider usage, blocks, alerts |
| `services.py` | Registration, lookups, applying results, scheduled checks, courier catalog |
| `webhooks.py` | Signature verification, durable storage, idempotent processing |
| `notifications.py` / `reminders.py` / `alerts.py` | Messages and administrative warnings |
| `views.py`, `urls.py`, `serializers.py` | REST API |
| `tasks.py` | Celery tasks; `migrations/0002` registers the Beat schedules |

## Internal versus external eligibility

The existing authoritative classification is used, not email domains:

* **Internal**: `UserType.get_internal_user_codes()` (IITR student, individual student, faculty, IITR startup).
* **External**: `UserType.get_external_user_codes()` (educational institute, Govt R&D, industry, external
  startup/MSME, other).
* A booking counts as external when `Booking.user_type_snapshot` is external, as the Accounts In Charge scope and the
  external sample workflow already do, **and** the booking user is still external now. If the two disagree (e.g. a
  user re-classified after booking), the booking is treated as not external: manual delivery only (fails closed).
* Only the booking's own user declares the arrival. Staff can view and refresh, never declare.
* Bookings accept a declaration while `PENDING`, `PENDING_PAYMENT`, `BOOKED` or `HOLD`.

Ambiguities found and how they were resolved:

1. Snapshot versus current user type: both must be external (above).
2. "Individual student" is internal: reports and the account lifecycle group it under IITR students.
3. No equipment flag says which bookings send samples, so any eligible booking may declare (as today's "Sample Sent").
4. "Lab in-charge" means the equipment's Lab Operators (`EquipmentOperator`); "Account in-charge" means the
   department's Accounts In Charge (FINANCE users of the equipment's department, falling back to another finance user,
   the same rule the booking page uses to display them).
5. Waitlist entries (which have their own "sample submitted" fields) are out of scope for the first release.

## Manual delivery

* **Internal users:** the form shows manual delivery only, with the reason. The backend refuses `COURIER` for them
  (403), whatever is sent directly to the API. A manual row has no courier fields (a database check constraint), is
  never looked up, gets no courier notifications or reminders, and consumes no quota. It records intent, not receipt,
  so it adds no "Sample Sent" event.
* **External users** choose courier or manual. Switching courier → manual keeps the courier record (inactive, with
  `superseded_at` and reason), stops its checks and reminders. Switching manual → courier re-checks eligibility and the
  department switch.
* One active declaration per booking (partial unique constraint). The booking row is locked during registration, so
  concurrent submissions serialise; an identical repeat updates the optional details instead of adding a row.
* **Frozen once the lab has the sample:** after any sample-trace event other than the user's own "Sample Sent" (Held at
  Office, Forwarded, Accepted, Processing, ...), the declaration can no longer be changed (403, with the reason shown).
  Otherwise a new "Sample Sent" would become the latest event and make the booking look not yet received, including
  to the operator-unavailable auto-refund job.
* **One "Sample Sent" per booking:** only the first courier registration writes it (none if the older "Sample Sent"
  dialog already did). Later courier changes are kept in the arrival history.
* **Limits:** at most 3 courier registrations per booking (changes included) and 5 per user per 24 hours (429).

## Department enablement

Courier Tracking is a module in **Department Modules** (`/admin/department-modules`). The switches, reasons, the
test-users-only pilot mode and the append-only history are the ones the other modules use.

* Migration `department_modules.0003_courier_tracking_module` adds the switch and seeds a row for every existing
  department: **on for IIC** (department code `IIC` or the name "Institute Instrumentation Centre", the same rule as
  the other modules' seeding), **off for every other department**. Each seeded row is audited.
* Departments created later start off (existing new-department behaviour).
* A department with **no row** counts as off unless it is IIC (`OFF_UNLESS_CONFIGURED`). The other modules keep their
  pre-switch "on" default.
* Only the Main Administrator can change a switch (existing department-modules API, reason required).
* The switch never overrides the internal-user rule.
* **Turning a department off:** no new courier registrations (manual stays available), no lookups and no reminders for
  its shipments. Existing records are kept and shown read-only. Webhook updates for them, which cost no quota, are
  still applied so a reported delivery still reaches the lab staff. Turning it back on resumes scheduled checks.

## Data model (migrations)

* `courier_tracking.0001_initial`: the tables above. Constraints:
  * `courier_one_active_arrival_per_booking` (unique `booking` where `is_active`);
  * `courier_fields_match_arrival_mode` (MANUAL ⇒ empty courier and number; COURIER ⇒ both present);
  * `courier_notification_once` (`arrival, kind, state_key, recipient, channel`);
  * `courier_reminder_once` (`booking, kind, sequence`);
  * unique `TrackingWebhookEvent.body_sha256` and `TrackingLookup.correlation_id`;
  * indexes for lookups by courier and number, due checks, department and status, user, ledgers and alerts.
* `courier_tracking.0002_templates_and_schedules`: four email templates and four Celery Beat tasks (reversible).
* `department_modules.0003_courier_tracking_module`: choices plus the seed (reverse removes only untouched seed rows).

The API key is never stored. The lookup ledger keeps a 16-character hash of the tracking number, not the number.
Webhook payloads (tracking number and checkpoints, no customer details) are stored for processing and audit; the
checkpoints are courier events and locations only.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TRACKCOURIER_API_KEY` | empty | API key. Empty: no tracking request is ever made (no ledger row, no block, no alert); registrations still work and show "not yet checked"; the admin page shows "No API key". |
| `TRACKCOURIER_API_BASE_URL` | `https://api.trackcourier.io/v1` | Must be HTTPS (the client refuses anything else). |
| `TRACKCOURIER_TIMEOUT_SECONDS` | 20 | Read timeout; connect timeout is min(5, this). |
| `TRACKCOURIER_WEBHOOK_SECRET` | empty | Signing secret. Empty: the webhook endpoint refuses every delivery (503). |
| `TRACKCOURIER_WEBHOOK_PREVIOUS_SECRET` | empty | Accept the previous secret for a while after rotating (the provider asks for ≥ 15 minutes). |

Set them in the server environment or secret store only, never in the repository, the frontend or logs. The test
settings force all three secrets to be empty, so the ordinary test suite can never reach the provider.

## API (`/api/v1/courier-tracking/`)

| Method and path | Who | Notes |
|---|---|---|
| `GET couriers/` | signed-in | Cached catalog from `GET /v1/couriers` (not counted against the quota), refreshed at most daily. 503 `catalog_unavailable` if it was never loaded; the UI then shows an error with Retry, never an empty list. |
| `GET bookings/<id>/arrival/` | booking user; staff in scope | Eligibility (`courier_allowed`, `default_mode`, reasons), active declaration, history. |
| `POST bookings/<id>/arrival/` | booking user only | Validates mode, eligibility, department switch, courier from the catalog, tracking number. 201 / 400 (`fields`) / 403. |
| `GET shipments/` | signed-in | Own records; `view=staff` for staff scope. Filters: `q` (booking, equipment, department, user, sample, number, courier), `department`, `equipment`, `booking`, `user`, `courier`, `status` (comma list), `arrival_mode`, `sample`, `expected_from`, `expected_to`, `synced_before`, `overdue`, `include_inactive`, `page`. |
| `GET shipments/<id>/` | owner or staff in scope | 404 otherwise (no existence leak). Staff also see the submitter and problem codes. |
| `POST shipments/<id>/refresh/` | owner (USER trigger) or staff in scope (STAFF trigger) | Rate-limited; 429 with the current record when refused. |
| `GET admin/overview/`, `PATCH admin/settings/`, `POST admin/alerts/<id>/resolve/`, `POST admin/clear-block/`, `POST admin/refresh-catalog/` | Main Administrator | Settings changes need a reason and are audited (`DepartmentModuleAuditLog`, action `courier.settings`). |
| `POST webhooks/trackcourier/` | TrackCourier.io (signature) | See Webhooks. |

**Staff scope:** Admin sees everything. OIC, Department Admin and Lab Operator see their equipment
(`_get_equipment_ids_for_log_access`, the existing rule). Accounts In Charge see external bookings (existing rule).

## Searchable courier selector (frontend)

`src/components/courier/CourierCombobox.tsx` reuses the portal's shadcn `Popover` + `Command` (cmdk); there is no
new dependency.

* **Search:** a search box ("Search courier name...") with the search icon. Matching is case-insensitive and partial,
  on the display name and the API slug, and ignores spaces when matching slugs. Best matches come first: names
  starting with the text, then a word of the name, then anywhere, then the slug. Examples: "blue" → Blue Dart,
  "india" → India Post and India Post International, "speed" → Speed Post, "international" → India Post
  International, "maruti" → Shree Maruti. No aliases are invented.
* **Keyboard:** the trigger is a `role="combobox"` button with `aria-expanded`, `aria-controls`, `aria-haspopup`,
  `aria-labelledby`, `aria-invalid` and `aria-describedby`. Enter, Space or a click opens it; arrow keys move; Enter
  selects; Escape closes. cmdk provides the listbox and option semantics, and the search input is named "Search
  courier name".
* **Mouse and touch:** the content is portalled (no clipping in cards or dialogs). It takes the trigger's width, is at
  most the viewport width minus 1.5 rem, and has a scrollable list (`max-h`, `overscroll-contain`) with 40 px rows.
* **Value:** shows the display name and stores the exact slug. A clear button sits next to it. Only list items can be
  chosen; free text never becomes a value, and the backend also rejects any slug not active in the catalog.
* **States:** loading (spinner, disabled trigger), error (alert with Retry; never an incomplete list), and
  "No matching courier found. Try a different name.".
* **Performance:** one request per page session, shared by every selector and in-flight request (`courierHelpers.ts`
  module cache; the app shell has no react-query provider). Filtering is local and memoised. No request is made per
  keystroke, and none for internal users or manual mode.

Other frontend pieces:

* `SampleArrivalForm` (mode choice, courier, number with the India Post format check, dates, identifiers, notes,
  "Email me when the courier tracking status changes.", duplicate-submit guard, server field errors);
* `SampleArrivalPanel` (on the booking detail card, above the sample lifecycle);
* `ShipmentStatus` (status, latest checkpoint, history, last and next check, the "reported delivery is not receipt"
  note);
* `pages/CourierTracking.tsx` (`/courier-tracking`: my shipments, and a staff view with filters);
* `pages/AdminCourierTracking.tsx` (`/admin/courier-tracking`, with a tile in Admin Settings);
* `departmentModulesApi.ts` (module key added, so the switch appears in Department Modules).

## Tracking-number validation

The number is normalised for every courier: spaces removed, upper case. Both the original and the normalised number
are stored.

* India Post, Speed Post, Registered Post and India Post International must match `^[A-Z]{2}\d{9}[A-Z]{2}$`, as
  documented.
* Other couriers get generic bounds only: 5–50 letters, digits, `-` or `/`. No courier-specific rule is invented.

## Quota and lookup policy

* **Provider facts:**
  * The free plan has 100 tracking requests a month.
  * `GET /v1/couriers` is not counted.
  * Every answer, errors included, carries `usage {used, quota, plan}`, which is authoritative.
  * Live webhooks are only on Growth and Business, so on the current plan updates come from scheduled lookups.
* **Ledger:** every `GET /v1/track` is a `TrackingLookup` row, reserved under a lock on the month's
  `TrackingQuotaMonth` row before the request, so concurrent requests cannot overspend.
* **Effective usage:** the larger of the provider's last reported `used` and the local conservative count, plus
  requests in flight.
* **What counts:** INVALID_TRACKING_NUMBER (documented "nothing was looked up or counted"), QUOTA_EXCEEDED and
  authentication errors count as not consumed. Everything else counts. Reservations whose request never completed
  count after 10 minutes.
* **Reserve:** a configurable reserve (default 10) is kept for the first lookup of a booking's first shipment and for
  staff refreshes. Scheduled checks, user refreshes and the first lookup of a re-registered shipment stop before it.
* **Booking status:** only shipments of BOOKED bookings are looked up. Unpaid (PENDING / PENDING_PAYMENT), held,
  cancelled or finished bookings keep their registration but cost no lookups; checks start once the booking is booked.
* **Blocks:** QUOTA_EXCEEDED pauses lookups until the month ends. INVALID_API_KEY / API_KEY_REQUIRED /
  ACCOUNT_NEEDS_ATTENTION pause them until the Main Administrator clears the block (audited). Each raises an admin
  alert. An alert also fires at the usage threshold (default 80%).
* **Months:** UTC calendar months; a new month starts a new row.
* **When lookups happen:**
  * once after registration (queued after commit, picked up by the scheduled run if the queue is down);
  * scheduled runs at 09:15 and 15:15, with a daily budget of the spare quota above the reserve divided by the days
    left;
  * priority order: never checked, then out for delivery or a problem, then the oldest check;
  * minimum intervals of 48 hours (in transit) and 12 hours (out for delivery or a problem);
  * at most 12 automatic checks per shipment (staff can still check).
* **When they don't:** never on page load. Never for terminal shipments (delivery reported, returned, cancelled,
  expired), manual or internal arrivals, or departments switched off.
* **Failures:** no blind retry. A transient failure waits for the next window. A tracking-details failure waits for the
  user to correct the details.
* **Duplicate suppression:** one parcel registered on several bookings costs one request; the answer updates every
  matching active shipment, and a fresh answer under an hour old is reused.
* **User refreshes:** at most once per 12 hours per shipment and 3 a day per user (429 otherwise).
* **Staff refreshes:** at most once an hour per shipment and 10 a day per staff member.
* **ARC Carrier and Om Logistics** are only followed when looked up, so they rely on the scheduled checks.
* **Uncertain quota:** fail safe. No request is made and the last known status is shown.

## Webhooks

The receiver is ready but unused on the free plan. Configure it only when the plan includes live webhooks.

* **URL:** `https://<portal>/api/v1/courier-tracking/webhooks/trackcourier/`. It never redirects. With no secret
  configured it answers 503 to everything.
* **Verification:** `X-Trackcourier-Signature: sha256=<hex>` is an HMAC-SHA256 of the exact raw body, compared in
  constant time, with the previous secret also accepted during rotation. Non-ASCII headers are rejected.
* **Bodies:** malformed JSON gets 400. A verified body is stored (`TrackingWebhookEvent`) before the 2xx response, then
  processed asynchronously; a periodic sweep processes anything left.
* **Deduplication:** by the SHA-256 of the body, which covers exact retries; the duplicate count is recorded. The
  portal never deduplicates by `X-Webhook-Delivery`, nor by event + courier + number + `enqueued_at`, which the
  provider says can collide.
* **Idempotency:**
  * a checkpoint array replaces the stored one; `checkpoints: null` keeps it;
  * a reported delivery is never reopened;
  * an event older than the newest one applied (`enqueued_at` against the arrival's `last_event_at`, lookups using
    their own time) is recorded but changes nothing.
* **Test deliveries** (`data.courier` starting `synthetic-test-`) are stored and ignored.
* **Unknown numbers** are recorded as `UNMATCHED`.

## Notifications

They use `CommunicationService.send_email` (template codes, test-account redirects) and the in-app notifications.
Every message is a `ShipmentNotification` row created first. The unique key makes repeats no-ops; sends happen after
commit, and the 10-minute sweep retries failures up to 5 times, then raises an alert.

* **User:**
  * In-app notice: on registration and once per meaningful status (in transit, out for delivery, delivery reported,
    attempt failed, exception, returned, cancelled).
  * Email: only if the user ticked "Email me when the courier tracking status changes." and status emails are enabled
    (global setting).
* **Staff:** when a delivery is reported, the equipment's OIC (incl. temporary OIC), Lab Operators and the department's
  Accounts In Charge are notified.
  * Email: always sent, whatever the user chose. Subject: `[IIC Booking] External shipment reported delivered —
    Booking <ref>`.
  * In-app: optional.
  * Recipients are deduplicated across roles.
  * A missing role raises a `RECIPIENT_MISSING` admin alert.
  * The message asks staff to verify physical receipt and update the sample lifecycle. The sample is not marked
    received.
* **Templates:** `courier_*` templates match no notification-preference category, so they are always delivered,
  except where a recipient's own settings decline them (recorded as skipped).

## Reminders

Reminders are decided daily at 10:30 from local data only (no provider call), for external bookings in enabled
departments that are still waiting for a sample (not cancelled or completed, and no Held / Forwarded / Accepted /
Rejected / Processing / Completed trace). In test-users-only (pilot) mode only test users are reminded.

"Arrival method not chosen" is further limited, so go-live does not remind every open booking:

* only BOOKED bookings (not unpaid or held);
* only bookings made after Courier Tracking was last switched on for the department (the seeding for IIC, otherwise
  the latest off → on change in the module audit log). If that time is unknown, no such reminder is sent;
* not for fabrication or walk-in equipment (no sample is sent);
* not when the booking already has a sample-trace event, e.g. "Sample Sent" from the older dialog.

| Kind | When |
|---|---|
| Arrival method not chosen | No declaration `arrival_reminder_after_days` (2) after booking |
| Courier details need attention | The provider could not track the details given |
| Not progressing | No status change for `stalled_after_days` (5) despite a recent successful check |
| Delivery problem | Attempt failed / exception |

Each kind is sent at most `reminder_max_count` (3) times per booking, at least `reminder_interval_days` (3) days
apart, and stops by itself once the condition clears. The wording is non-accusatory, notes that courier updates can be
delayed, and never claims a shipment is lost. Manual arrivals and internal users get no courier reminders.

## Celery Beat (created by `courier_tracking.0002`)

| Task | Schedule |
|---|---|
| `courier_tracking.scheduled_checks` | 09:15, 15:15 |
| `courier_tracking.sweep` (notifications + webhook events) | every 10 minutes |
| `courier_tracking.reminders` | 10:30 |
| `courier_tracking.refresh_catalog` | 03:30 |

## Local setup and testing

Backend:

```
cd D:\IIC_NEW\wt-be-courier
set DJANGO_READ_DOT_ENV_FILE=False & set PYTHONDONTWRITEBYTECODE=1
set DATABASE_URL=postgresql://iic_istem_test@127.0.0.1:5432/iic_courier   # pytest uses test_iic_courier
python -m pytest iic_booking/courier_tracking iic_booking/department_modules -p no:cacheprovider -q
```

The provider is a scripted fake at the HTTP layer (`requests.get` inside the client), so the real client code runs
and no request reaches TrackCourier.io.

Frontend:

```
cd D:\IIC_NEW\wt-fe-courier
npm ci
npx vitest run src/components/courier
npx tsc --noEmit -p tsconfig.app.json
npx eslint src/components/courier src/pages/CourierTracking.tsx src/pages/AdminCourierTracking.tsx
npx vite build
```

**Manual local check (not executed).** Run the backend with no `TRACKCOURIER_API_KEY`. Then:

1. Register a courier shipment as an external test user on an IIC booking. It shows "Registered (not yet checked)";
   "Check now" answers that checks are not switched on yet, and the admin page shows "No API key".
2. Register as an internal user: manual delivery only.
3. Switch IIC off in Department Modules: courier registration is refused.
4. Sign a webhook body with a local test secret and POST it to the receiver, then check the shipment and the staff
   notification rows.

## Deployment, rollback and troubleshooting

* **Prerequisites:**
  * set `TRACKCOURIER_API_KEY` in the secret store;
  * leave the webhook secret empty on the free plan;
  * run `migrate` (three migrations) after review;
  * Celery Beat with the database scheduler (already used);
  * confirm the real quota from the first lookup's `usage` on the admin page;
  * keep only IIC on and pilot with "test users only" first if wanted;
  * validate the first external shipment with an approved test shipment;
  * enable other departments only on explicit Main Administrator decision.
* **Rollback:**
  * code: revert the branch;
  * migrations, from Department Modules: switch Courier Tracking off everywhere. Then `migrate courier_tracking zero`,
    which drops the tables (export them first if the history is needed), and `migrate department_modules 0002`,
    which removes untouched seed rows and restores the choices. Rows an administrator changed are left in place;
    they are harmless, because choices are not a database constraint and the reverted code never reads them.
* **Troubleshooting:**
  * "not yet checked" forever → the API key is missing ("No API key") or a block is set (admin page);
  * the courier list is unavailable → the provider is unreachable (alert `CATALOG_UNAVAILABLE`); Retry, or "Refresh
    courier list";
  * no staff email → look for `RECIPIENT_MISSING` and `NOTIFICATION_FAILED` alerts and the
    `courier_tracking_shipmentnotification` rows;
  * a webhook with 401 → secret mismatch or rotation.

## Known limitations

* Proof-of-delivery files (Growth plan and above) are not used; `PODImageUrl` is deprecated and ignored.
* Waitlist entries are not covered.
* The local quota count errs high on purpose; the provider's `usage` corrects it on the next answer.
* The `q` search uses `icontains` across joined tables: fine at portal scale, not indexed.
