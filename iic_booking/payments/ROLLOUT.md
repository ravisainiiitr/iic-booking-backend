# Razorpay payment module — rollout notes

## Environment

Set these on the server (GitHub Actions secrets / `.env`):

| Variable | Required | Notes |
|----------|----------|--------|
| `RAZORPAY_KEY_ID` | Yes | Dashboard → API Keys |
| `RAZORPAY_KEY_SECRET` | Yes | Dashboard → API Keys |
| `RAZORPAY_WEBHOOK_SECRET` | Yes (prod) | Dashboard → Webhooks → secret |

## Webhook

1. In Razorpay Dashboard → **Webhooks**, add:
   - URL: `https://equip.iitr.ac.in/api/payments/razorpay/webhook/`
   - Events: `payment.captured`, `payment.failed`, `refund.processed`, `refund.failed`, settlement events if available
2. Copy the webhook secret into `RAZORPAY_WEBHOOK_SECRET` **before** enabling production traffic.
3. Verify + webhook both call the same idempotent `settle_order_success` path.

## Migrate

```bash
python manage.py migrate payments
python manage.py migrate users  # PaymentGateway.RAZORPAY choice
```

Fee defaults are seeded at 0% convenience fee and 18% GST on fee.

## Admin

Main Admin → Equipment → Booking Charge Settings → **Razorpay convenience fee**, or:
- `GET/PATCH /api/admin/payments/fee-settings/`

## Deprecations

- `POST /api/payments/sbiepay/initiate/` → **410 Gone** (use Razorpay create-order).
- Offline UTR / finance receipt path unchanged.
- Historical SBIePay `PaymentGatewayTransaction` rows remain read-only.
- Legacy `POST /api/wallet/razorpay/*` still exists; UI uses `/api/payments/razorpay/`.

## Settlements sync (optional)

```bash
python manage.py sync_razorpay_settlements
```

Schedule via Celery beat / cron daily for SBI bank UTR reconciliation (`PaymentSettlement.bank_utr`).

## Existing PENDING_PAYMENT bookings

Pay via the new Razorpay path on `/bookings/:id/payment`. `amount_due` is unchanged until settle.
