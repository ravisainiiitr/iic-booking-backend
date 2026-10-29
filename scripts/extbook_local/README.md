# Local acceptance environment: external booking

For testing the external-booking enhancement on this machine only. Nothing here touches production, staging, shared
databases, S3, Razorpay, SBIePay, SRIC, I-STEM, SMTP/IMAP or any other external service.

## Safety: what is enforced, and where

| Control | Where |
|---|---|
| Explicit opt-in: `IIC_ACCEPTANCE_ENV=local-acceptance`; refuses containers (`/.dockerenv`, Kubernetes) and CI | `config/settings/acceptance_guards.py` `check_environment` |
| `DATABASE_URL` must be PostgreSQL on 127.0.0.1 / localhost / ::1, database exactly `iic_extbook_accept_dev`, no URL options; only one database configured | `check_database_url`, `acceptance_local.py` |
| Every new database connection: the server reports a loopback address and the database carries the disposable marker (`COMMENT ON DATABASE ... 'iic-extbook-local-acceptance:disposable'`), which only `create-db` / `snapshot` / `restore` set | `check_connected_database` (Django `connection_created`) |
| Drop or copy over only the two marked databases (`iic_extbook_accept_dev`, `iic_extbook_accept_dev_seeded`); an existing database is never adopted | `extbook-local.ps1` `Assert-Disposable` |
| No `.env` is read; AWS keys, profiles, credential/config files and instance metadata are neutralised | `acceptance_local.py` |
| E-mail: console backend by default; `-MailFiles` writes to `.acceptance_local/mail` only; SMTP host forced to 127.0.0.1:9 | `acceptance_local.py`, `guard.py` |
| Razorpay: no key id / secret, so `create_order`, the legacy wallet client and checkout verification refuse before any call; local-only webhook secret | `acceptance_local.py` |
| SBIePay: no merchant; gateway URL `http://127.0.0.1:9/...` | `acceptance_local.py` |
| SRIC API key, OpenAI key empty; Ollama to a dead local port; channels in memory | `acceptance_local.py` |
| Outbound network: DNS lookups and socket connections to anything but loopback raise `OutboundNetworkBlocked` (covers requests/urllib3, Razorpay SDK, boto3, smtplib, imaplib, web push, Guacamole) | `install_network_guard`; tests in `iic_booking/external_services/tests/test_acceptance_guards.py` |
| Celery: tasks run inline (local.py); worker/beat processes refused; database beat schedule never used; scheduled jobs only via `task` | `acceptance_local.py` |
| Browser runs: every request not to 127.0.0.1/localhost is aborted and logged | test harness (scratchpad), see below |

`guard.py` (run before every command) prints only the target host, port and database name and the integration
state, never a credential, and refuses if any check fails.

## First-time setup

```powershell
cd D:\IIC_NEW\wt-be-extbook
.\scripts\extbook_local\extbook-local.ps1 create-db    # creates and marks iic_extbook_accept_dev
.\scripts\extbook_local\extbook-local.ps1 migrate      # about 10 minutes
.\scripts\extbook_local\extbook-local.ps1 seed         # synthetic users, equipment, slots, holiday, local feature switches
.\scripts\extbook_local\extbook-local.ps1 snapshot     # marked copy used by "restore"
```

## Run

```powershell
# backend: http://127.0.0.1:8000/api/  (simulated clock just after the original 28 Oct 2026 21:00 opening)
.\scripts\extbook_local\extbook-local.ps1 backend -Clock 2026-10-28T21:05:00+05:30
# frontend: http://127.0.0.1:8080  (other worktree; proxies /api to 127.0.0.1:8000; refuses if VITE_API_URL is set)
D:\IIC_NEW\wt-fe-extbook\scripts\extbook_local\start-frontend.ps1
```

* Without `-Clock` the real clock is used.
* The clock starts at the given instant and then ticks. The Windows clock is never changed.
* `restore` resets the data to the seeded state. Stop the backend first.

## Accounts

Created by `seed`, all with addresses ending `@acceptance.invalid`:

| Account | Role |
|---|---|
| `ext.academic`, `ext.academic2` | External academic |
| `ext.industry` | External industry, wallet funded |
| `student` | Internal student |
| `faculty` | Internal faculty |
| `oic` | Officer In Charge of both synthetic equipment items |
| `operator` | Lab operator |
| `accounts` | Accounts In Charge (FINANCE) |
| `admin` | Main Administrator |

* The shared password is generated on the first seed and is in `.acceptance_local/credentials.txt`, which is local
  and git-ignored.
* Internal accounts use the "Sign in with email" profile switch, because Channel i is not available locally.
* Feature switches are set only in this database, through the normal admin service functions, so the change logs
  record them. The two policy texts stay unapproved.

## Mock payments

`mock_razorpay.py` loads only under the acceptance settings and replaces only the gateway boundary, in its own process:

* `razorpay.Client` and `get_razorpay_client` are replaced by a fake client.
  * `order.create` returns `order_MOCK...` ids.
  * Refund and settlement calls raise.
* Synthetic keys (`rzp_test_ACCEPTANCELOCAL`) are set only inside `MockGateway.active()`.
* Events are delivered to the real `/api/payments/razorpay/webhook/` (signed with the local secret) and `/verify/`
  (checkout signature).
* Like the gateway, the mock captures only orders it created, and only for the order amount.
* `raw_event` sends anything else, to test how the application treats it.

```powershell
.\scripts\extbook_local\extbook-local.ps1 pay <booking_id> [-Outcome success|failure] [-Clock <time>]
.\scripts\extbook_local\extbook-local.ps1 payments      # all payment scenarios (run after restore)
.\scripts\extbook_local\extbook-local.ps1 race 20       # expiry vs settlement (20 x 3), open-limit race, checkout races (last slot; one wallet, two checkouts)
```

The application refuses a signed `payment.captured` / `payment.authorized` event that would settle an unpaid order
unless its amount (paise) and currency equal the order's (`razorpay_service._webhook_amount_problem`). This is a
minimal guard; `fix/payment-security` (D:\IIC_NEW\wt-be-finance) replaces it with gateway confirmation and
reconciliation. On merge, keep that branch's version.

### Booking checkout in the browser (external self-bookings)

External self-bookings go through a payment choice; nothing is held before payment (design doc section 4a). To use
online payment in the browser, start the backend with the mock gateway in its process. Razorpay Checkout itself (CDN)
is never loaded locally: the browser run stubs it, and the payment is delivered by webhook:

```powershell
.\scripts\extbook_local\extbook-local.ps1 backend -MockGateway -Clock 2026-10-28T21:05:00+05:30
# book as ext.academic (wallet 0), ext.academic2 or ext.industry (wallet funded); then, for an online choice:
.\scripts\extbook_local\extbook-local.ps1 pay-checkout <checkout id from the URL> [-Outcome success|failure]
```

Wallet-only payments need no gateway. `paid_not_confirmed` checkouts (slot taken before the payment arrived) are listed
read-only in Django admin under External booking checkouts.

## Other helpers

```powershell
.\scripts\extbook_local\extbook-local.ps1 check
.\scripts\extbook_local\extbook-local.ps1 task expire_special_allocations -Clock <time>
.\scripts\extbook_local\extbook-local.ps1 task queue_completed_invoice_requests
.\scripts\extbook_local\extbook-local.ps1 task sric_invoice_batch -Clock 2026-12-10T09:31:00+05:30 -MailFiles
.\scripts\extbook_local\extbook-local.ps1 acceptance -QrPython <py> -QrScript <decode_label_qr.py>   # API end-to-end
```

Simulated payments and test-mode SRIC messages are evidence of application behaviour on synthetic data only; they are
never evidence that a payment was realised or an invoice requested.

Do not commit this folder, `config/settings/acceptance_*.py` or `.acceptance_local/` without review. They are local
tooling.
