# External booking enhancement: technical design

Status: implemented on `feature/external-booking` (backend `D:\IIC_NEW\wt-be-extbook`, frontend
`D:\IIC_NEW\wt-fe-extbook`), baseline tag `v2.5.47.275` / `5fe7d69`. Not committed, not deployed. Every feature is
**off by default**; with everything off the portal behaves as on the baseline, except the security fix in §9.

All code lives in the new app `iic_booking/external_services` (label `external_services`). Existing files change only
where a hook is unavoidable; each such change is listed in §10.

## 1. Time zone and conventions

* Portal time zone: `Asia/Kolkata` (`TIME_ZONE`; Celery uses the same). All instants are timezone-aware; dates are IST
  calendar dates. IST has no daylight saving, but all computations use `zoneinfo` regardless.
* Every change by staff or the Main Administrator is recorded in an append-only table (rows cannot be updated or
  deleted: `procurement_management.AppendOnlyModel`).
* Messages to users and staff go through `ExternalNotification` (§8): created inside the business transaction, sent
  after commit, never from `save()`.

## 2. External booking window

### Configuration (`ExternalBookingWindowConfig`, single row)

| Field | Meaning |
|---|---|
| `enabled` | The policy applies. Off: external users keep the previous per-equipment rule. |
| `initial_open_at` | First opening instant (e.g. Wed 28 Oct 2026 21:00 IST). |
| `first_week_start` | Monday of the first open week (e.g. 9 Nov 2026). |
| `initial_weeks` | Weeks open at the first opening (4). |
| `auto_expand`, `expansion_weekday`, `expansion_time` | One more week opens at every Wed 21:00 IST. |
| `materialized_expansions`, `counting_from` | Bookkeeping for pause / resume / schedule changes. |
| `manual_weeks` | Weeks opened early by the Main Administrator. |
| `min_lead_days` | Earliest bookable day = today + this (0 by default). |

No date is hard-coded: the IIC schedule is entered through the Main Administrator API. The migration does not
enable anything.

### Rules (`window.compute`)

```
open           = enabled and now >= initial_open_at
expansions     = materialized_expansions + #{weekly instants in (counting_from, now]}   (while auto_expand)
last_date      = first_week_start + 7 * (initial_weeks + expansions + manual_weeks) - 1
first_date     = max(first_week_start, today + min_lead_days)
bookable(d)    = open and first_date <= d <= last_date          (both ends inclusive)
```

* An expansion takes effect exactly at its instant (`>=`).
* Correctness never depends on a task: a missed 21:00 run changes nothing for users.
* Enabled but without dates: closed (fails closed).

| From (IST) | Open to external users |
|---|---|
| before Wed 28 Oct 2026 21:00 | nothing |
| Wed 28 Oct 2026 21:00 | Mon 9 Nov - Sun 6 Dec 2026 |
| Wed 4 Nov 2026 21:00 | 9 Nov - 13 Dec 2026 |
| Wed 11 Nov 21:00 | to 20 Dec (past days drop off at the start) |
| Wed 18 Nov 21:00 | to 27 Dec |
| Wed 25 Nov 21:00 | to Sun 3 Jan 2027 |
| Wed 2 Dec 21:00 | to Sun 10 Jan 2027 |

The 2-8 Nov week is never open to external ordinary bookings. These rows are the tests in
`tests/test_window_rules.py::test_boundary_table`.

### Administration (`window_admin`)

Main Administrator only (`department_modules.access.is_main_admin`: ADMIN user type or superuser). Every change
needs a reason (≥ 10 characters), runs under a row lock on the configuration, and is recorded with the configuration
and the bookable range before and after.

* **Shortening** the open range (fewer weeks, a later opening, disabling) needs `confirm_reduction`, and is refused
  while any active external booking (PENDING, PENDING_PAYMENT, BOOKED, HOLD) falls after the new last date. No change
  ever cancels a booking.
* **Pause**: the expansions so far are kept; weekly expansions stop. **Resume**: counting restarts at the resume time;
  expansions that fell during the pause are not caught up (decision D3).
* **Advance** opens 1-8 extra weeks immediately, on top of the schedule.
* Changing the opening resets the counting to the new opening (a backdated opening counts the missed expansions);
  changing the weekday or time keeps the expansions so far and counts the new schedule from the change.
* **Preview** shows the range before and after a proposed change without saving.

### Enforcement

* `ExternalSlotQuotaService.get_external_slot_window_date_bounds` returns the central window when enabled (an empty
  range while closed). This is the one choke point for the slots grid, booking templates, equipment groups.
  `mode_availability` and `template_slot_preference` use the central opening times.
* Server-side guard (`guards.py`) for external booking users, inside the slot lock, on both booking paths of
  `_book_equipment_impl`, both reschedule views and waitlist auto-booking. Refusal: HTTP 400, `code:
  EXTERNAL_WINDOW_CLOSED`, the open range, the dates outside it, `special_request_available: true`.
* Exempt (unchanged behaviour): internal users; the Main Administrator / admin-panel users and staff allowed to skip
  period limits when booking for a user. Equipment downtime, maintenance, holidays, weekends, department rules,
  equipment groups, quotas, prices and cancellation rules are untouched: the window only decides whether an external
  user may request an ordinary slot on a date.

### Preparation task (`external_services.prepare_window`, every 15 min)

Records each open week once (`ExternalBookingWindowOpening`, unique week), generates its missing slots (same helper and
options as the Wednesday next-week task) and then takes the external quota snapshot for that week. Repeated or
concurrent runs do nothing twice (unique rows, per-step timestamps, rows claimed with `skip_locked`). The slots of the
week about to open are generated up to 1 h before it opens.

## 3. Special booking requests (external users)

Reuses `equipment.UrgentBookingRequest` with a new type `EXTERNAL_SPECIAL` and a new status `CLARIFICATION`.

**Isolation.** The model's default manager (`objects`) now excludes `EXTERNAL_SPECIAL`. Every existing urgent-request
query (about 32 sites: screens, caps, approvals, supervisor flow, expiry, reports) therefore behaves exactly as before
and cannot see or act on an external request (an internal endpoint given an external request id answers 404).
External code uses `UrgentBookingRequest.all_types`. External-only data is in `SpecialRequestDetail` (1:1),
`SpecialRequestEvent` (append-only history) and `SpecialAllocation` (1:1 with the booking).

**Submission** (`special_requests.create`):

* External users only; feature switch `special_requests_enabled`; the same portal lock, department switch, equipment
  status and training authorisation gates as ordinary booking.
* Tentative preferred time (future, ≤ 24 h long, within a year) plus up to 3 alternatives, sample count and description,
  remarks. Booking inputs are cleaned with the same function as Type B urgent requests.
* Estimate with the charge engine and the user's own charge profile, GST for external users; the urgent surcharge only
  when `special_request_surcharge` is on (decision D7).
* At most `max_open_special_requests` (3) pending / clarification requests per user, counted under a row lock on the
  user.
* Nothing is reserved: no slot, no booking, no quota.
* The client cannot set status, amounts, decision fields or timestamps (only the listed inputs are read).

**Short notice** (`is_short_notice`): `slot_start - now < short_notice_days × 24 h` (12 days). Exactly 12 days is not
short notice. Checked against the preferred start at submission and again against the allocated slot at allocation.
Acknowledgement needs the current `PolicyText` version for `short_notice`; stored: version, SHA-256 of the exact text,
server time. A client flag alone is never accepted.

**Review** (`special_staff`): staff who may decide urgent requests (`check_operator_permission`), for equipment in
their charge (`_get_equipment_ids_for_log_access`; the Main Administrator sees all), never their own request.
Clarify (request → CLARIFICATION; the user's reply returns it to PENDING), reject (reason required), list candidate
slots (with weekend / holiday / status notes), allocate.

## 4. Allocation, payment deadline, expiry and restoration

**Allocation** (one transaction, request and slots locked):

1. Request must be PENDING with no allocation; training authorisation re-checked.
2. Slots validated by the urgent-allocation validator (not booked, not past, no overlap with another mode). Weekend,
   holiday and maintenance slots may be chosen deliberately; they are flagged.
3. The amount is re-quoted; `expected_total` (what the OIC saw) must match.
4. External weekly quota checked **without bypass** under the existing snapshot row lock.
5. `deadline = min(approved_at + payment_window_hours (24), first slot start)`, both server-side.
6. A PENDING_PAYMENT booking is created with `amount_due = total`; slots BOOKED; previous slot statuses recorded; the
   request becomes APPROVED; history, booking event and notifications.

No wallet is debited (decision D9).

**Quota while awaiting payment.** `count_external_usage` also counts the slots of special allocations awaiting payment,
so ordinary bookings cannot use quota already allocated. Ordinary PENDING_PAYMENT bookings are still not counted, as
before (decision D6).

**Payment.** Only the existing settlement paths count:

* Razorpay signature verification or webhook (`payments.razorpay_service._settle_booking_shortfall`);
* Finance processing an offline UTR receipt (`users/api/payment_views.finance_payment_receipt_process`);
* legacy SBIePay settlement (`users/payment_settlement`).

Each calls `special_payment.settlement_hook` with the booking locked; for any other booking it returns True and
changes nothing.

* On time → allocation PAID, booking confirmed as usual.
* Gateway confirmations up to 15 minutes after the deadline count (the order could only be created before it).
* Offline receipts count if submitted by the deadline.
* Later money → the booking is expired now (if it was not already) and the payment is recorded as a **late payment**
  for Finance; the booking is never revived and no refund is made automatically.

Starting a payment (`create_order`) is refused after the deadline, for an expired allocation, or before the
short-notice conditions are accepted for a short-notice slot.

**Expiry** (`external_services.expire_special_allocations`, every 5 min): for each allocation 15 minutes past its
deadline, one transaction with the allocation (`skip_locked`) and booking locked.

* If the booking shows a settled payment or a PAID gateway order → PAID, never cancelled.
* Otherwise: slots released, booking CANCELLED (cancellation reason `PAYMENT_DEADLINE_EXPIRED`), request EXPIRED,
  history and notifications, waitlist notified for freed AVAILABLE slots.
* Repeated or concurrent runs do nothing twice.

**Restoration.** Each slot goes back to the status it had before allocation (weekend / holiday NOT_AVAILABLE, BLOCKED,
SCHEDULED_MAINT stay closed), except: equipment now under maintenance → UNDER_MAINTENANCE (the existing rule); a
previously AVAILABLE slot on a holiday declared since → NOT_AVAILABLE.

## 4a. Booking checkout: payment choice, nothing held before payment

External self-bookings (an external user booking for themselves, with a charge) no longer create an unpaid booking that
holds slots. `POST /api/equipments/<id>/book/` refuses them with `409 external_checkout_required` at the point where
the booking would be written, after every existing check; the frontend then opens the checkout.

**Flow** (`external_services/checkout.py`, `checkout_context.py`, model `ExternalBookingCheckout`):

1. `POST /api/v1/external-booking/checkouts/` runs the normal booking pipeline (`_book_equipment_impl`) in *quote*
   mode: slots locked and re-checked, window, quotas, limits, price (with GST). It stops before any write and returns
   the total `R`. Nothing is held.
2. The page offers options from `R` and the eligible wallet `W`. `W` is what the sub-wallet may spend (existing
   minimum-balance and block rules) minus the wallet parts of the user's other checkouts awaiting online payment.
   * `W = 0`: online only (one button, no choice).
   * `0 < W < R`: full online `R`, or wallet `W` + online `R − W`.
   * `W ≥ R`: full online `R`, or full wallet `R` (balance can end at 0).
3. `POST …/checkouts/<id>/pay/ {choice}`. The client sends only the choice. The server re-quotes (a changed price
   gives `409 price_changed`) and locks the sub-wallet while it computes `W`, so concurrent choices serialise.
   * **Full wallet:** the pipeline runs in *confirm* mode in one transaction. It re-checks the slots and price,
     debits `R`, and creates the booking as BOOKED. No gateway order. A failure debits nothing (`409`).
   * **Online options:** a Razorpay order is created for `R − W` (plus the existing convenience fee) through the
     existing `create_order` (`checkout=` argument). No slot lock is held during the gateway call. The checkout
     waits 15 minutes for payment; until then its wallet part counts against the balance offered to other checkouts.
4. **Settlement:** the existing Razorpay paths (webhook, checkout verify) reach `settle_order_success`, then
   `_settle_booking_shortfall`, then `settle_checkout_order`, with the order row locked.
   * The pipeline runs in *confirm* mode: slots, maintenance, holidays, window, quota and price are checked again,
     and the wallet part `W` is debited in the same transaction as the booking.
   * The order is linked to the booking, so existing cancellation refunds find the gateway payment.
   * If the booking can no longer be made (slot taken, maintenance, price changed, wallet spent), the captured payment
     stays recorded (`Payment` row, order `PAID`). The checkout becomes `paid_not_confirmed` with the reason, and
     nothing is refunded automatically (existing policy; Finance refunds).
   * A late payment, after expiry or cancel, is still applied if the booking can still be made.
5. The confirmation page shows only the server state, never the browser callback alone. It polls for about a minute
   while the gateway confirms.

**Amount checks:** the gateway amount is the order total (`R − W` + fee). The webhook guard (and, after merge, the
finance branch's `captured_payment_problem`) compares against that, not `R`.

**Unchanged:** internal users and staff bookings on behalf of a user. Bookings by staff for an external user still
create `PENDING_PAYMENT` as before. Also unchanged: waitlist confirmation (full wallet only), special allocations
(own deadline), wallet top-ups, and every payment callback.

## 5. Invoices

The portal never issues a GST invoice; SRIC does.

* **Provisional invoice** (`documents.provisional_invoice_pdf`): titled "Provisional Invoice / Performa Invoice — Not
  a GST Tax Invoice". Amounts from the booking, or the special-request estimate (marked as an estimate). Owner, the
  department's Accounts In Charge, or staff in scope only.
* **Preference** (`InvoicePreference`, append-only): "after analysis", or "early" when `early_invoice_allowed`. Early
  needs the current `early_invoice` disclosure version (version and text hash stored). A later change of mind is a new
  row and never retracts a request already queued or sent.
* **One request per booking** (`SricInvoiceRequest.booking` is unique): early request (explicit, payment realised) or
  completion (`queue_completed`, every 30 minutes: completed, paid external bookings with an "after analysis"
  preference, or with none when `auto_request_without_preference` is on). Repeats hit the same row.
* **Snapshot** at queue time from authoritative records: billing profile (missing billing details stop the request
  with a message; nothing is invented), GSTIN if present, facility (equipment department), equipment, booking
  reference, FBR number if present, charge breakdown and total, Razorpay and offline payment references, completion
  status, timing and early acknowledgement.

### Daily batch (`external_services.sric_invoice_batch`)

Runs at 09:30 and every 30 minutes until 17:00 on weekdays.

* Working day = Monday-Friday and not an active `Holiday` (the portal's holiday calendar; decision D14).
* The first run of a working day opens that day's batch (unique per date) and takes every request queued before it.
* Each request is claimed (BATCHED/FAILED → SENDING) and emailed outside any transaction, one email per request:
  "GST Invoice Request — {FBR} — {booking ref}".
* Success → SENT (accepted by the mail server; **not** "invoice issued").
* Failure → FAILED with backoff 30 min × 2ⁿ, up to 5 attempts, then GAVE_UP and an alert to the Accounts In Charge.
* A worker that dies mid-send leaves SENDING → UNCERTAIN after 30 min, never resent automatically; staff resolve it
  (`confirm_sent` or `resend`, audited).
* Mode `off` (default) sends nothing; `test` sends to the test address with a `[TEST]` subject; `live` needs the SRIC
  address (decision D13). There is no user-facing "send now".

### Upload and download

Accounts In Charge = FINANCE users of the equipment's department; the Main Administrator may also act.

* PDF only (`%PDF-` signature), ≤ 10 MB, booking reference or FBR must match the request.
* Stored with the default (private, signed-URL S3) storage under `sric_invoices/`; SHA-256 recorded.
* Downloads only through `/invoices/documents/<id>/download/` (owner: current document; staff in scope: all),
  `Cache-Control: no-store`.
* Replacement keeps the old document (`is_current=False`) with a reason.
* One notification per document (unique key).

## 6. Printed invoice copies and labels

* **Address book** (`SavedPostalAddress`): owner only (others get 404). Validated server-side (Indian PIN: 6 digits,
  not starting with 0). One default. An address used for a dispatch is archived rather than deleted.
* **Copy request** (`PhysicalCopyRequest`): needs an invoice request. Waits (`waiting_invoice`) until the invoice is
  uploaded, then the Accounts In Charge are notified. The address is copied into the request. Stages: requested →
  prepared → dispatched (date not in the future, Speed Post reference), each audited; the user is notified on
  dispatch. One open copy request per invoice.
* **Labels** (`documents.py`): 10 × 4.5 in page (720 × 324 pt, verified by test), 0.25 in margins, text shrunk to fit
  with a visible ellipsis as a last resort. The IIC address is the Main Administrator's `iic_postal_address` setting;
  labels are refused while it is empty (decision D16). Logo: the repository's `fonts/iitr-pdf-masthead.png`. Print at
  100% / Actual size with "Fit to page" off, and check the first print with a ruler.

## 7. QR sample receipt

* The external owner creates a label for a waiting booking. A new label revokes the previous ones.
* The QR code holds `FRONTEND_URL/staff/sample-receipt?code=<token>`: a random 128-bit token. Only its SHA-256 is
  stored. The printed 8-character reference is random; no personal data is in the code.
* **Lookup** (POST, so the token is not in API URLs or access logs): staff who may set office sample statuses for that
  booking (the same rules as `set_booking_sample_status`). It changes nothing; the look-up is audited.
* **Confirm** (explicit, separate): records Forwarded to Lab, or Held at Office with a reason, through
  `BookingSampleTrace` under the booking lock. A booking already received is not recorded again (audited as
  duplicate). Courier delivery information and label creation never count as receipt.

## 8. Notifications

`ExternalNotification` rows have a unique key (event, recipient, channel). They are sent after commit, in-portal
always and by email with the `external_service_notice` template. A row is claimed by compare-and-swap; failures are
retried by `external_services.notifications_sweep` (every 10 min, ≤ 5 tries); a row stuck mid-send becomes UNCERTAIN
and is not resent.

## 9. Security

* Every endpoint requires authentication. Role checks are server-side; object-level checks answer 404 rather than 403
  where existence would otherwise leak.
* Users cannot decide their own requests. Slots, timestamps, deadlines, statuses and staff identities are never taken
  from the client.
* **Fixed (always on): external hold bypass.** `create_as_hold=true` on the booking API created a HOLD booking that
  skipped the weekly and external quotas and was never released for external users. It is now refused for external
  users (`EXTERNAL_HOLD_NOT_ALLOWED`) unless staff exempt from period limits book for them.
* No secrets in code or logs. Billing details and files are excluded from Django admin lists. Tokens are stored
  hashed.
* **Fixed (always on): webhook amount and currency.**
  * A correctly signed `payment.captured` / `payment.authorized` event used to settle its order whatever amount or
    currency it carried.
  * An event that would settle an unpaid order is now refused (HTTP 400, nothing recorded) unless its amount in paise
    and its currency equal the order's (`_webhook_amount_problem`).
  * Replays for an already-paid order are unchanged.
  * This is a minimal guard: `fix/payment-security` replaces it with gateway confirmation and reconciliation.
* Staff and owner actions authorise (role, scope, ownership) before validating the caller's input, so an
  unauthorised caller always gets the same 403/404.

## 10. Changes to existing files

| File | Change |
|---|---|
| `config/settings/base.py` | App registered |
| `config/api_router.py` | Routes `v1/external-booking/`, `v1/admin/external-booking/` |
| `equipment/models.py` | `EXTERNAL_SPECIAL` type, `CLARIFICATION` status, default manager excluding external requests, `all_types` |
| `equipment/cancellation_models.py` | `PAYMENT_DEADLINE_EXPIRED` reason |
| `equipment/migrations/0255_…` | Choice-only alterations for the three fields above |
| `equipment/external_slot_quota.py` | Central window delegation; awaiting-payment special allocations count toward usage |
| `equipment/api_views.py` | Window guard (2 booking paths, 2 reschedule views), structured refusal, external hold refusal, slots API `external_window` |
| `equipment/template_slot_preference.py`, `equipment/mode_availability.py` | Central window for external viewers |
| `equipment/waitlist_booking.py` | Window guard |
| `payments/razorpay_service.py` | Payment-start refusal and settlement hook for special allocations; webhook amount/currency guard; `create_order(checkout=…)` for a checkout's online part; `settle_checkout_order` call when a shortfall order has no booking |
| `equipment/api_views.py` (checkout) | `external_checkout_required` for external self-bookings at the write point; quote / confirm modes through `checkout_context` |
| `users/api/payment_views.py`, `users/payment_settlement.py` | Settlement hook |

### Overlap with other branches

* **Payment security (`wt-be-finance`)**: changes `payments/razorpay_service.py`, `payments/models.py` and
  `equipment/api_views.py`. The hooks here are small, self-contained imports and calls, so they rebase easily.
  * That branch (uncommitted work in `D:\IIC_NEW\wt-be-finance`, inspected read-only) already validates captured
    payments against the order (`captured_payment_problem`, with a reconciliation status and migration).
  * The webhook guard added here (`_webhook_amount_problem` and its call in `handle_webhook`) is a subset. On merge,
    take `fix/payment-security`'s `handle_webhook`, then re-run
    `external_services/tests/test_payment_webhook_integrity.py`.
  * Those tests expect HTTP 400 for a mismatch. Adjust them if that branch answers differently, for example 200
    with the order flagged for reconciliation.
* **Booking checkout vs payment security**: on merge, keep `fix/payment-security`'s `confirm_and_settle`,
  `_settle_captured_payment` and reconciliation, and carry over two small pieces.
  1. `create_order`'s `checkout=` branch, unchanged.
  2. The no-booking branch at the top of `_settle_booking_shortfall`. That branch returns a problem string, so call
     `settle_checkout_order(order)`:
     * `None`: not a checkout order;
     * `""`: settled;
     * otherwise: return the reason as the problem, so the order is flagged `RECONCILIATION_REQUIRED`.
  Then re-run `external_services/tests/test_booking_checkout*.py`. Its webhook tests expect HTTP 200 plus
  `paid_not_confirmed` for a slot taken meanwhile; with the finance branch the order is also flagged.
* **Courier tracking (`wt-be-courier`)**: writes sample-trace events and freezes arrival declarations once the lab has
  the sample. The QR receipt here records the same office statuses through `BookingSampleTrace` and is compatible.
* **I-STEM (`wt-be-istem`)**: no overlap in files.
* `equipment` migrations: both this branch and others add `0255`; a merge migration will be needed at integration.

## 11. Migrations

| Migration | Content |
|---|---|
| `equipment.0255_urgent_request_external_special_choices` | Choice-only `AlterField` on 3 fields |
| `external_services.0001_external_booking_window` | Window models |
| `external_services.0002_prepare_window_schedule` | Beat row (15 min) |
| `external_services.0003_special_requests` | Settings, policy texts, special-request models, notifications |
| `external_services.0004_special_request_seed` | Proposed short-notice wording (`approved=False`), email template, expiry and sweep beat rows |
| `external_services.0005_invoices_delivery_receipt` | Invoice, copy, address, label and receipt models; settings fields |
| `external_services.0006_invoice_seed` | Proposed early-invoice disclosure (`approved=False`), invoice beat rows |
| `external_services.0007_booking_checkout` | `ExternalBookingCheckout` (new table only; required because no booking exists until payment) |

All are additive and reversible. Production must first have applied every migration up to `equipment.0254` and
`communication.0063` (the baseline release); check with Plan Production Migrations before deploying.

## 12. Deployment preparation (nothing has been deployed)

**Environment variables:** none added. Email uses the existing `DEFAULT_FROM_EMAIL` and email backend. Storage uses the
existing default storage (private S3 with signed URLs). Frontend links use the existing `FRONTEND_URL` helper.

**Configuration after deployment** (Main Administrator, `/admin/external-booking`): the window dates and switch; the
feature switches; SRIC recipient and test addresses; the IIC postal address for labels; approved policy wording (add a
new approved version). All are audited.

**Order:**

1. Merge with the payment-security branch first (or rebase onto it).
2. Add a merge migration for `equipment` if another branch also added `0255`.
3. Tag a `v2.*` release from master.
4. Deploy the backend, then run Plan Production Migrations and `migrate-production.yml`. The code tolerates the gap
   before migrating.
5. Deploy the frontend (push to master).
6. Configure, test in `test` dispatch mode, then switch features on one by one.

**Workers:** Celery worker and beat. Six beat rows are created by migrations and do nothing while their feature is
off: prepare window (15 min), expire special allocations (5 min), notification sweep (10 min), queue completed invoice
requests (30 min), SRIC batch (09:30-17:00 weekdays), plus the existing ones.

**Payment callbacks:** unchanged (Razorpay webhook and verify). Special allocations hook into the existing settlement.

**Monitoring:**

* Admin page: last prepared week.
* Django admin, read-only: allocations past their deadline, notifications in FAILED/UNCERTAIN, SRIC requests in
  FAILED/GAVE_UP/UNCERTAIN.
* Accounts page: failed items.
* Log lines: `external_window.prepare`, `external special expiry`, `sric invoice batch`.

**Smoke test (staging):**

* Window off: an external user books as before.
* Configure a window with a past opening on a test department; an external test user sees the range; a direct POST
  outside it gets `EXTERNAL_WINDOW_CLOSED`; an internal user is unaffected.
* Submit a special request; the OIC allocates; check the deadline and the payment page. Let one expire and check the
  slot status.
* Queue an invoice request with dispatch in `test` mode and check the test mailbox at 09:30 on a working day.
* Upload a PDF; the user downloads it.
* Request a printed copy; print the envelope label and measure it.
* Create a sample label; scan it as Accounts In Charge; confirm receipt.

**Internal booking regression checklist:** book, reschedule and cancel as a student and as faculty; internal urgent Type
A and Type B (create, approve, expire); weekly slot opening countdown; quotas and wallet debits; sample statuses for
internal bookings.

**Rollback:**

* Switch the features and the window off (behaviour returns to today's immediately), then redeploy the previous tag if
  needed.
* The migrations are reversible (`migrate external_services zero`, `migrate equipment 0254`). Reverse them before
  removing the code, and only if the data can go; otherwise leave the tables.
* With the window off, special allocations already made still expire or settle normally while the code is deployed.

## 13. Decisions awaiting approval

| # | Decision | Who approves | What the code does until then |
|---|---|---|---|
| D1 | Actual opening date/time (28 Oct 2026 is past by deployment) and what external users may book until the switch is on | IIC administration | Window off: the previous rule applies |
| D2 | Minimum lead time for external ordinary bookings | IIC administration | `min_lead_days = 0` (configurable) |
| D3 | Expansions during a pause: skipped or caught up | Main Administrator | Skipped |
| D5 | Whether pending special requests consume quota | IIC administration | Pending requests consume nothing; quota is checked at allocation and allocations awaiting payment count |
| D6 | Whether ordinary unpaid external bookings should count toward the quota | IIC administration | Unchanged (not counted) |
| D7 | Urgent surcharge for external special requests; per-user open-request limit | IIC administration | No surcharge; limit 3 (configurable) |
| D8 | Short-notice wording and the "no refund" sentence | IIC administration | Proposed wording stored as unapproved v1; feature off |
| D9 | Wallet use for special allocations | IIC finance | Not used; full amount due by gateway / UTR |
| D10 | Late payment after expiry: refund handling | IIC finance | Recorded and flagged; no automatic refund |
| D11 | Early GST invoice wording, eligible categories, GST treatment on refund | SRIC / Institute finance | Proposed wording stored as unapproved v1; early option off |
| D12 | Which booking/payment states qualify for an invoice request; final amount and GST breakdown | SRIC | Completed + payment realised; booking's recorded charge breakdown |
| D13 | SRIC recipient address and required fields; one email per request | SRIC | Dispatch off; address empty |
| D14 | Is the portal `Holiday` table the official institute calendar? Who maintains it? | IIC administration | Used as the calendar |
| D15 | Who uploads, replaces and dispatches invoices | IIC finance | Accounts In Charge (FINANCE of the department) and the Main Administrator |
| D16 | Official IIC sender/recipient address for labels; logo approval | IIC administration | Labels refused until the address is set; existing crest masthead |
| D17 | Physical copies for all external categories | IIC finance | Any external booking with an invoice request (feature off) |
| D18 | Allocating closed (weekend/holiday) slots counts toward the weekly external quota, which is a share of bookable slots | IIC administration | Counts (existing quota rule) |
| D19 | Refund of a captured payment whose booking could not be confirmed (slot taken during checkout) | IIC finance | Recorded (`paid_not_confirmed`, read-only in Django admin); refunded manually under existing rules |
| D20 | Offline UTR / bank-deposit payment for external self-bookings (it needs a held booking) | IIC finance | Not offered in the checkout; still available for staff-created and special-allocation bookings |
| D21 | Staff booking on behalf of an external user: keep the unpaid hold or move to the checkout | IIC administration | Unchanged (unpaid `PENDING_PAYMENT` hold) |
| D22 | Online payment window per checkout | IIC finance | 15 minutes (late payments still applied if the slot is free) |
| D23 | Razorpay Dashboard branding (business name, logo, brand colour) | SRIC / IIC finance | Not changed; Checkout options set name, logo, description and theme per payment |
