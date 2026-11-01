# Payment and Account Security Hotfix (milestone M0)

- **Branch:** `fix/payment-security` (worktree `D:\IIC_NEW\wt-be-finance`), based on `origin/master` `5fe7d69`.
- **Scope:** the first milestone of the Razorpay / wallet / finance programme. The full audit and plan are in `D:\IIC_NEW\razorpay-finance-audit-and-plan.md`.
- **Status:** implemented and tested locally. **Not committed, not deployed. No production data, configuration or credentials were changed, and no real payment or refund was made.**

## Problems fixed

| ID | Problem | Where |
|---|---|---|
| C1 | `POST /api/wallet/razorpay/verify-payment/` credited the **amount sent by the browser**, not the order amount, with no row lock. A ₹1 order could credit ₹1,00,000 (confirmed in a rolled-back probe). | `users/api/wallet_views.py` |
| C2 | Any signed-in user could PATCH their own `user_type` (e.g. `admin`), `admin_approved`, `use_discounted_charge_profile`, `is_active`, `department`, … through `/api/users/<id>/`; admin-panel staff could do the same to other users (confirmed in a rolled-back probe). | `users/views/user_views.py` |
| C3 | Any operator could refund any Razorpay payment, of any amount, through `POST /api/payments/razorpay/refund/`. Refund accounting ignored **pending** refunds, so a pending full refund plus a retry could over-refund. `Payment` was marked REFUNDED before Razorpay processed the refund, and refund webhooks never updated it. | `payments/views.py`, `payments/razorpay_service.py` |
| C4 | Wallet-to-wallet transfers were **on by default** (`WalletSricSettings.peer_transfer_enabled` default `True`) and allowed any wallet-eligible recipient, including external users. | `users/models/wallet_sric_settings.py`, `users/wallet_peer_transfer.py` |
| — | The forgot-password OTP had no request limit and no wrong-guess limit. Sign-up had no rate limit. Email uniqueness was case-sensitive. | `users/api/auth_views.py`, `users/api/auth_serializers.py` |

## Behaviour after the fix

### C1: legacy wallet Razorpay endpoints
- `POST /api/wallet/razorpay/create-order/` **never creates an order**: 403 `online_gateway_recharge_disabled` when online recharge is off (the existing contract), otherwise **410 Gone** pointing to `/api/payments/razorpay/create-order/`. The portal UI uses only `/api/payments/razorpay/`.
- `POST /api/wallet/razorpay/verify-payment/` remains so legacy orders paid before the change can still settle:
  - the HMAC of `order_id|payment_id` is checked with `hmac.compare_digest`;
  - inside one transaction, the `WalletRazorpayOrder` row is locked (`SELECT … FOR UPDATE`), must belong to the caller's wallet, and must have a positive amount;
  - an optional client `amount` must equal the stored `amount_paise` exactly;
  - the credit is the stored `amount_paise`, never a client value;
  - the order row is deleted in the same transaction.
- A concurrent or repeated verification waits for the lock, then finds no order and gets 400, so each order credits at most once. Any refusal inside the locked block rolls back. `SubWallet.credit` itself uses an atomic `F()` increment.

### C2: user updates
- `UserViewSet.update`: **only Admin** may edit other users (Admin behaviour unchanged). Everyone else may edit only their own record, and only `SELF_EDITABLE_FIELDS`: `name`, `gender`, `phone_number`, `secondary_phone_number`, `wallet_low_balance_alert_enabled`, `wallet_low_balance_alert_threshold`, `istem_portal_acknowledged`, `auto_slot_selection`, `user_guide_viewed`. These are the fields the Profile page sends.
- Any other key gives **400** with the list of refused fields, and nothing is saved.
- Other write paths were reviewed and need no change:
  - `config/admin_api.py` `UserAdminViewSet` checks every create, update and delete through `_assert_can_manage_user_payload` (Admin; organisation admins within their organisation and external types; department admins within their department with RBAC grants, never Admin or Dept-Admin accounts);
  - `identity_admin_views` is Admin-only;
  - `user_roles_views` is read-only;
  - `profile_me` is read-only;
  - registration and approval flows are separate, permissioned endpoints;
  - `UserSerializer` has no writable nested fields.

### C3: refunds
- **Standalone endpoint:** `POST /api/payments/razorpay/refund/` is limited to **Main Admin and Finance**.
- **Booking refunds:** operators, OICs and department admins keep refunding through the booking refund flow (`equipment/api_views.py` `refund_booking` → `refund_booking_internal`). That flow requires the RBAC `bookings.manage` grant, limits OICs to their own equipment (`_oic_booking_scope_denied`), and refunds only the payment linked to that booking.
- **`create_refund`:**
  - locks the payment row;
  - counts **PENDING and PROCESSED** refunds toward the captured amount;
  - a full refund (`amount=None`) already covered by pending or processed refunds returns the existing refund instead of calling Razorpay again;
  - a full refund after a partial one refunds only the remainder.
- **`Payment.status`** now reflects **processed** refunds only (`recompute_payment_refund_status`).
- **Refund webhooks** (`refund.processed` / `refund.failed`) lock the refund, update it, and recompute the payment. A late or replayed `refund.failed` cannot undo a processed refund. A failed refund frees the amount for a new refund.

### C4: wallet-to-wallet transfers
- **New setting** `WALLET_TRANSFER_APPROVED_CATEGORY_PAIRS` (environment, comma-separated `sender_user_type>recipient_user_type`, e.g. `faculty>faculty`). **Default: empty, so no combination is approved.**
- `peer_transfer_enabled()` (used by both peer-transfer endpoints) is true only when the admin switch is on, the department allows it, **and** at least one combination is approved.
- `wallet_peer_transfer.assert_category_pair_approved` is enforced in `create_pending_transfer` **and again** in `confirm_transfer_with_otp`. So even a direct service call, or a transfer created before an approval was withdrawn, is refused (`peer_transfer_category_not_approved`) without moving money.
- The user-facing wallet settings report `peer_transfer_enabled: false` until a combination is approved. The admin switch and per-department mode states are unchanged.
- **Effective default:** off in every environment unless `WALLET_TRANSFER_APPROVED_CATEGORY_PAIRS` is set. The database column default (`True`) was **not** changed, because the setting already makes the effective default off without a schema change. Changing the column default is an optional later migration.
- `transfer_between_sub_wallets` (moving money between one user's own department sub-wallets) and SRIC recharge transfers are not wallet-to-wallet transfers between users and are unchanged.

### Forgot-password OTP, sign-up and email
- **Forgot-password OTP:** at most 5 requests per address per 15 minutes (429 `OTP_RATE_LIMITED`), and 5 wrong codes per OTP, after which the OTP is deleted (`OTP_ATTEMPTS_EXCEEDED`). The OTP expiry stays 10 minutes. The email is lower-cased for the cache key and matched case-insensitively. This reuses the login-OTP helper and constants.
- **Sign-up:**
  - `_registration_attempt_allowed` limits attempts per client IP per hour (429 `REGISTRATION_RATE_LIMITED`);
  - the client IP comes from `_registration_client_ip`, which trusts `X-Forwarded-For` only for `REGISTRATION_TRUSTED_PROXY_HOPS` proxies counted from the right, so entries a client prepends are ignored.
  - **Both settings default to 0 (limit off, header not trusted)**; see the deployment prerequisites.
- **Email:** `RegisterSerializer.validate_email` checks uniqueness case-insensitively. Stored emails are not changed.

## Configuration added

| Setting | Default | Purpose |
|---|---|---|
| `REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR` | `0` (off) | sign-up attempts per client IP per hour |
| `REGISTRATION_TRUSTED_PROXY_HOPS` | `0` | proxies in front of Django that append to `X-Forwarded-For` |
| `WALLET_TRANSFER_APPROVED_CATEGORY_PAIRS` | empty | Institute-approved transfer combinations |

`config/settings/test.py` sets `REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR = 0`, because the in-process cache is shared across the test run; the dedicated tests switch it on.

## Deployment prerequisites

1. **Proxy chain for the sign-up limit.** The repository shows Traefik (`compose/production/traefik/traefik.yml`, no `forwardedHeaders.trustedIPs`) and workflows that manage an Apache virtual host for `equip.iitr.ac.in`. Confirm the actual chain in front of Django (e.g. Apache → Traefik → Django, or Traefik → Django) and whether each proxy **appends** or **replaces** `X-Forwarded-For`. Then set `REGISTRATION_TRUSTED_PROXY_HOPS` to the number of appending proxies and only after that enable `REGISTRATION_MAX_ATTEMPTS_PER_IP_PER_HOUR` (suggested 20). With hops = 0 behind a proxy, every user shares the proxy's address and one limit; with hops set too high, a forged header could evade it.
2. **Frontend:** no change is required. The Profile page sends only allowed fields, and the UI already uses `/api/payments/razorpay/`. Confirm no external integration calls the legacy wallet Razorpay routes.
3. **Wallet transfers:** leave `WALLET_TRANSFER_APPROVED_CATEGORY_PAIRS` unset until the Institute approves specific combinations.
4. **Razorpay webhook:** make sure `refund.processed` and `refund.failed` are subscribed, so pending refunds reach their final status.

## Historical reconciliation and data review (read-only; Finance / Admin)

Nothing was changed in any database. Run these in a Django shell against production **only with authorisation**.

```python
# C1 — every credit made by the legacy endpoint (its description uses a plain hyphen; the trusted
# /api/payments path writes "Recharge via Razorpay — Order:" with an em dash).
from iic_booking.users.models import SubWalletTransaction
rows = SubWalletTransaction.objects.filter(description__startswith="Recharge via Razorpay - Order:")
for t in rows.order_by("created_at"):
    print(t.created_at, t.pk, t.sub_wallet_id, t.amount, t.description)
# Finance: match each order/payment ID against the Razorpay Dashboard and compare the credited amount with
# the captured amount. The legacy order rows were deleted after crediting, so the Dashboard is the only
# source of the paid amount. Any difference is a financial discrepancy to resolve under Finance procedures.

# C1 — legacy orders still awaiting verification (they will now credit the stored amount when verified).
from iic_booking.users.models.wallet import WalletRazorpayOrder
print(WalletRazorpayOrder.objects.count())

# C2 — accounts whose privileged fields should be confirmed against approval records.
from iic_booking.users.models import User
print(list(User.objects.filter(user_type="admin").values_list("pk", "email", "date_joined")))
print(list(User.objects.filter(use_discounted_charge_profile=True).values_list("pk", "email", "user_type")))

# C3 — refunds and payments to recheck after deployment (payments marked REFUNDED while a refund was pending).
from iic_booking.payments.models import PaymentRefund
print(list(PaymentRefund.objects.filter(status="PENDING").values_list("razorpay_refund_id", "payment__razorpay_payment_id", "amount")))

# C4 — whether transfers ran, and between which categories.
from iic_booking.users.models.wallet import WalletPeerTransfer
print(list(WalletPeerTransfer.objects.filter(status="COMPLETED").values_list("pk", "sender__user_type", "recipient__user_type", "amount", "updated_at")))
```

There is no field-change history for users, so C2 can only be checked against approval records and the expected admin list.

## Known limitations (not changed in this hotfix)

- The database `email` constraint is case-sensitive. Two simultaneous sign-ups differing only in case could both pass. Fix: a case-insensitive unique index, after a production check for existing case-duplicates (proposed, not migrated).
- Booking-refund authority for operators and department admins with `bookings.manage` isn't scoped to equipment or department (OICs are). This is a policy decision.
- External users with a non-public email domain are auto-approved on email verification (existing policy in `users/api/auth_views.py`).
- Broader Razorpay integrity items (payment fetch on verify, `payment.authorized` handling, webhook event log, order expiry and reconciliation, late payments) belong to milestone M1.

## Test results (2026-10-25)

### Test databases

This worktree runs against its own dedicated test database:

- `DATABASE_URL=postgresql://iic_istem_test@127.0.0.1:5432/iic_finance`, which pytest-django turns into **`test_iic_finance`**. The database is created on first use, and only this worktree uses it.
- The I-STEM worktree keeps `.../iic_istem`, i.e. **`test_iic_istem`**, with its configuration unchanged.

Before the change, both worktrees shared `test_iic_istem`. That database also holds the I-STEM branch's `istem` tables, so transactional tests from this branch couldn't flush it, and their committed rows broke later tests. `test_iic_istem` was rebuilt once from the I-STEM worktree with `pytest iic_booking/istem --create-db`, after confirming three things:
- it is a pytest-managed test database (owner `iic_istem_test`);
- no other session was connected;
- no test process was running.

The I-STEM suite then passed 256/256 on it.

Every run used these options:
- `DJANGO_READ_DOT_ENV_FILE=False`
- `PYTHONDONTWRITEBYTECODE=1`
- interpreter `D:\IIC_NEW\wt-be-istem\.venv\Scripts\python.exe`
- pytest options `-p no:cacheprovider -q -W ignore`
- addopts `--ds=config.settings.test --reuse-db --import-mode=importlib`, from `pyproject.toml`

### Results on `test_iic_finance`

| Run | Command | Result |
|---|---|---|
| Hotfix tests | `pytest iic_booking/users/tests/test_security_hotfix.py iic_booking/payments/test_security_hotfix.py iic_booking/users/tests/test_wallet_transfer_approval.py` | **51 passed**, exit 0 |
| Full backend suite | `pytest iic_booking -rf --junitxml=...` | 182 failed, 5151 passed, 12 skipped, 1 error, exit 1 (20 min 49 s) |

Classification of the full-suite outcome:

- **182 failures:** all are on the list of failures already recorded for master `5fe7d69` before this hotfix. They are pre-existing.
- **1 error:** `equipment/tests/test_booking_template_preferred_slot.py::test_concurrent_auto_next_submits_get_distinct_slots` failed at teardown ("couldn't be flushed"). It's a thread-based concurrency test. It passed when rerun on the hotfix (3 runs, one showing a log line instead of the summary) and passed 3/3 on master. **Flaky, not a regression.**
- **Regressions: none.** The two tests that failed against an earlier hotfix version (the legacy create-order contract in `test_wallet_mode_switches` and `test_wallet_payment_modes`) pass with the final code.

On the shared database, before isolation, a master-vs-hotfix comparison of the affected modules was also identical apart from one setup collision that passed on rerun.

Existing test changed: `users/tests/test_wallet_mode_switches.py::test_defaults_keep_current_behaviour` asserted that wallet transfers are **on** by default. C4 changes exactly that, so the assertion now expects **off**. No other existing test was changed.

## Rollback

Revert the change set on `fix/payment-security`. There are no migrations or data to undo. If you roll back:
- keep the legacy `/api/wallet/razorpay/*` routes unreachable, for example by blocking them at the proxy, because the old verify code is exploitable;
- set `WalletSricSettings.peer_transfer_enabled = False` operationally, because the old code allows transfers by default.
