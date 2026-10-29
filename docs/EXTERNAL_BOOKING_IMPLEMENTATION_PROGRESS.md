# External booking enhancement: implementation progress

Do not trust this file alone: check `git -C D:/IIC_NEW/wt-be-extbook status` and rerun the tests before continuing.
Nothing here is committed, pushed or deployed. Design and decisions: `EXTERNAL_BOOKING_TECHNICAL_DESIGN.md`.

## Worktrees and environment

| | Backend | Frontend |
|---|---|---|
| Path | `D:\IIC_NEW\wt-be-extbook` | `D:\IIC_NEW\wt-fe-extbook` |
| Branch | `feature/external-booking` | `feature/external-booking` |
| Baseline | `5fe7d693bdef` (tag `v2.5.47.275`) | `95bfa6ab9952` |
| Environment | `.venv` (Python 3.13.14; `uv sync --frozen --inexact` from `uv.lock`, uv 0.10.4 inside the venv) | `npm ci` (Node 24.13.1, npm 11.8.0) |

Disposable local PostgreSQL test databases (never shared or production):
`iic_extbook` → `test_iic_extbook` (regression runs) and `iic_extbook_dev` → `test_iic_extbook_dev` (development).

```
cd D:\IIC_NEW\wt-be-extbook
set DJANGO_READ_DOT_ENV_FILE=False & set PYTHONDONTWRITEBYTECODE=1
set DATABASE_URL=postgresql://iic_istem_test@127.0.0.1:5432/iic_extbook_dev
.venv\Scripts\python -m pytest iic_booking/external_services -p no:cacheprovider -q     (add --create-db after model changes)
```

Never run `makemigrations` without checking its output: the baseline has unrelated pending choice drift on
`equipment` models, and Django writes an extra `equipment` migration for it. Delete that file and keep
`equipment.0255` as the only `equipment` migration of this branch.

## Stages

| Stage | State |
|---|---|
| 1 Baseline and environment | Done |
| 2 Booking window | Done (backend) |
| 3 Special requests | Done (backend) |
| 4 Approval, payment deadline, expiry | Done (backend) |
| 5 Invoice request queue and batch | Done (backend) |
| 6 Invoice preferences, upload, download | Done (backend) |
| 7 Physical copies, addresses, labels | Done (backend) |
| 8 QR sample receipt | Done (backend) |
| 9 Frontend | Done (see "Frontend" below) |
| 10 Regression | Done (final runs below) |
| 11 Review | Done: one concurrency defect found and fixed (expiry lock order); migrations checked forward/back |

## Test results (latest)

| Command | Result |
|---|---|
| Baseline: 186 equipment test files + `payments/tests.py` on `5fe7d69` | 2401 passed, 39 failed (20 min) |
| Same 39 on a pristine `git archive` of `5fe7d69` | 38 failed, 1 passed (`test_operator_report_revenue`: order-dependent in the full run) |
| Same set after stages 2-4 | 2400 passed, 40 failed: the 39 baseline failures + 2 new in `test_external_slot_quota` (DB-free tests now hit the window lookup), since fixed (tests now patch the central window off, see below) |
| `iic_booking/external_services` (after the final lock-order fix) | 139 passed |
| **Final**: baseline set + `payments/tests.py` + `external_services` | **2541 passed, 38 failed; no new failures** (the 38 all fail on pristine `5fe7d69`; the order-dependent 39th passed) |
| Migrations on a throwaway DB | forward, `external_services zero` + `equipment 0254`, forward again: all exit 0; `makemigrations external_services --check`: no changes; `manage.py check`: no issues |
| Frontend baseline `vitest run` | 249 files, 1975 tests passed |
| **Final** frontend `vitest run` | **251 files, 1994 tests passed** (exit 0) |
| Frontend `tsc --noEmit -p tsconfig.app.json` | exit 2, 141 errors: same files and counts as the baseline (none in new code) |
| `eslint` on new frontend files | 0 errors, 5 `react-refresh/only-export-components` warnings (pages export small helpers used by tests) |
| `ruff --select F,E9,B` on `external_services` | no F/E9 errors; 20 B904 (`raise ... from`) style findings |

## Frontend

* `src/lib/externalBookingApi.ts` (+ test): API client and pure helpers (IST formatting, countdown, short-notice
  boundary, PIN check). Base URLs are built lazily so screens importing the components have no import-time effects.
* `src/hooks/useExternalWindow.ts`, `src/components/external/ExternalWindowNotice.tsx`: on the booking page, for
  external users while the central window applies: open dates, next opening (IST), "Request a Special Booking"
  (carries the booking inputs from the booking form). Replaces the per-equipment countdown only in that case.
* `src/components/external/ExternalInvoicePanel.tsx`: on the booking details card for the external owner: provisional
  invoice, GST invoice timing (with the early disclosure), status, official download, printed copy with saved
  addresses, sample label.
* Pages: `/external-special-requests` (+ `/new`), `/external-special-requests/manage` (OIC), `/admin/external-booking`
  (Main Administrator), `/accounts/invoice-requests` and `/accounts/invoice-dispatch` (Accounts In Charge),
  `/staff/sample-receipt` (QR lookup and confirmation; camera via the browser's BarcodeDetector where supported, no
  new dependency).
* Changed existing files: `src/pages/BookEquipment.tsx`, `src/components/BookingDetailCard.tsx`,
  `src/routes/AppRoutes.tsx`.
* Not done: links to the new pages from the dashboard/navigation menus (reachable by URL and from notifications), an
  external-user dashboard summary section.

## Known failures

The 39 baseline failures (38 reproduce on pristine code; the 39th is order-dependent). They are in fabrication,
printing, maintenance templates, charge e-mail switch, migration-command tests and 4 urgent-expiry permission tests;
none is touched by this work.

## Changed existing tests

* `equipment/test_external_slot_quota.py::ExternalSlotWindowBoundsTests`: a `setUp` patches the central window to
  "disabled" (these database-free tests check the previous per-equipment rule, which applies only while the central
  window is off). Assertions unchanged.

## Migrations

`equipment.0255` (choice-only) and `external_services.0001`-`0006`. Applied and reversed only on disposable test
databases.

## Security findings and fixes

* External hold bypass via `create_as_hold` (fixed, always on).
* The date window was not enforced server-side (now enforced when the central window is enabled).
* Deploy safety: every read of the new tables on existing paths (window, quota count, payment hooks) falls back to the
  previous behaviour if the tables do not exist yet (deploys do not migrate).

## Unresolved policy questions

D1-D18 in the technical design, "Decisions awaiting approval".

## Review findings fixed in stage 11

* Expiry and payment settlement locked the booking and the allocation in opposite orders (possible deadlock); expiry
  now locks booking then allocation, skipping a booking a settlement holds.
* PATCH of another user's address returned a validation error before the ownership check; ownership is now checked
  first and PATCH is a partial update.
* Notification links pointed to non-existent routes (`/bookings/<id>`); now `/my-bookings?booking=<id>`.
* Invalid admin times such as `25:00` raised a 500; now a 400 with the field.

## Local acceptance testing (27 Oct 2026)

Environment, safety controls and instructions: `scripts/extbook_local/README.md`.
* Disposable database: `iic_extbook_accept_dev`, with the seeded snapshot `iic_extbook_accept_dev_seeded`. Both
  carry the disposable marker.
* Settings: `config.settings.acceptance_local`, with the guards in `config/settings/acceptance_guards.py`.

Second pass (hardened settings):
* explicit opt-in, container/CI refusal and URL-option refusal;
* disposable-marker check on every connection;
* DNS and socket guard, with tests;
* console e-mail by default;
* SBIePay disabled;
* the AWS credential chain neutralised;
* beat disabled;
* the mock Razorpay gateway (`mock_razorpay.py`) with the payment scenarios.

| Run (latest, hardened settings) | Result |
|---|---|
| Guard unit tests (`test_acceptance_guards.py`) | 22 passed |
| Settings refusal cases (no opt-in, CI, remote host, shared dev DB, URL options, missing URL) | all refused |
| Live: database without the disposable marker (restore, Django connection) | both refused |
| Payment scenarios 1-10 + checkout verify + expiry/settlement race (`extbook-local.ps1 payments`) | 55 / 55 checks passed; 4 observations (below) |
| API end-to-end (`extbook-local.ps1 acceptance`) | 87 / 87 checks passed |
| Browser (headless Chrome via playwright-core, clean data, parts 1-6, workflows 1-18) | 68 / 68 checks passed |
| Browser requests to non-local hosts (all aborted) | only Google Fonts and a Wikipedia-hosted logo; no API or payment host |
| `external_services` tests | 165 passed |
| Backend regression, fresh test DB, after this pass | 38 failed, 2567 passed; failure list identical to pristine `5fe7d69` |
| Frontend vitest | 251 files, 1994 passed (one unrelated `EquipmentFlashMessages` test failed once under CPU load, passed 3/3 alone and in the full rerun) |
| Frontend tsc | 141 errors, identical to baseline |

Payment observations (existing payment code, not changed):
* A correctly signed webhook whose amount differs from the order settles it. The event amount is not compared with
  the order.
* A second captured payment for an already-paid order is not recorded.
* Lateness is judged at callback time plus 15 minutes, not by the gateway's capture time.
* Booking ids in event notes are ignored; the order binds the booking.

Defects found and fixed in this pass:
* Invoice upload validated (and read) the uploaded file before checking that the user may handle the request. It now
  authorises first.
* The same order was applied to `clarify`, `reject`, `allocate`, `staff_confirm`, `mark_dispatched` and SRIC
  `resolve`.
* Tests were added for these.

Earlier pass:

| Backend regression, fresh test DBs (`--create-db`), after the lock-order fix: current worktree | 38 failed, 2541 passed |
|---|---|
| Same file set on pristine `5fe7d69` (fresh test DB) | 38 failed, 2402 passed; identical failure list |

A run that reused the test database (`--reuse-db` is in `pyproject.toml`) gave 17 failed / 2562 passed, including 4
`external_services` failures (`policy_missing`). The reused database had lost rows seeded by data migrations. Compare
results only on fresh test databases.

Defect found and fixed during acceptance: the sample-label section required a saved address, but the only place to
save one appeared after a GST invoice request. A first-time external user could not print a label before analysis.
The label section now asks for the sender address itself (`ExternalInvoicePanel.tsx`).

Expectations corrected in the test scripts (behaviour as designed):
* a non-staff lookup of a label code returns 404 (the label is not disclosed);
* a duplicate receipt confirmation returns `recorded: false`;
* a short-notice acknowledgement given at submission carries over to the allocation.

## Final independent verification (27 Oct 2026)

| Check | Command | Result |
|---|---|---|
| PowerShell syntax | `Parser::ParseFile` on both `.ps1` | 0 errors |
| Python syntax / lint | `py_compile`; `ruff --select F,E9,B` | exit 0; only the 20 earlier B904 style findings |
| Database identity | `psql` read-only queries | both acceptance DBs on 127.0.0.1:5432, owner `iic_istem_test`, disposable marker present; 9 users, all `@acceptance.invalid`; no non-synthetic bookings, payments, addresses or billing profiles |
| Running backend | startup guard output + synthetic sign-in | `config.settings.acceptance_local` on `iic_extbook_accept_dev` |
| Webhook amount/currency guard + regression tests | `pytest external_services/tests/test_payment_webhook_integrity.py` | 20 passed; with the guard disabled, 10 of them fail (mutation check) |
| Payment scenarios | `extbook-local.ps1 payments` | 57 / 57 |
| Expiry vs settlement races + open-request limit race | `extbook-local.ps1 race 20` (three batches) | 180 rounds, 0 problems; limit race: exactly 3 of 5 accepted |
| API end-to-end | `extbook-local.ps1 acceptance` | 87 / 87 |
| Pending model changes | `makemigrations --check --dry-run`, current vs pristine | identical lists (pre-existing drift only; none in `external_services`) |
| Migration reversibility | forward → `external_services zero` + `equipment 0254` → forward → `check`, on a throwaway DB (dropped) | all exit 0 |
| Backend regression (fresh test DB) | baseline file set + `payments/tests.py` + `external_services`, `--create-db` | **38 failed, 2589 passed**; pristine `5fe7d69`: 38 failed, 2402 passed; identical failure list; no new failures |
| Frontend | `vitest run` / `tsc --noEmit -p tsconfig.app.json` / `eslint` | 251 files, 1994 passed / 141 errors, same files and counts as baseline (exit 2) / no new findings |

Changes in this pass:
* webhook amount/currency guard (`payments/razorpay_service.py`);
* owner check before input validation in `respond`;
* UDP `sendto` added to the network guard;
* new tests:
  * `test_payment_webhook_integrity.py`;
  * the transition and authorisation-order tests in `test_special_requests.py`;
  * the guard test;
* `race_scenarios.py` and the `race` command;
* documentation updates.

## Next actions (for the human review)

1. Review the decisions D1-D18 with IIC / SRIC and record approved wording as new approved policy versions.
2. Add menu links to the new pages (dashboard / admin hub / accounts hub) once the pages are approved.
3. Plan the merge with `fix/payment-security` (same settlement files) and the `equipment` 0255 merge migration.
4. Staging smoke test per the technical design §12.
