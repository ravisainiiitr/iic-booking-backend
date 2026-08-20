# Wallet Credit Facility — Operations Runbook

## Controlled rollout

1. Deploy code with `WALLET_CREDIT_FACILITY_V2_ENABLED=false` (default).
2. Apply migration `users.0098_wallet_credit_facility_v2` on **staging only** first.
3. In Django admin → Wallet credit policy: set limits, then `enabled=True`.
4. Set env `WALLET_CREDIT_FACILITY_V2_ENABLED=true` on staging / pilot.
5. Smoke: faculty request → admin Channel-I review → reduce/approve → post credit → repay → CLEARED.
6. Production: keep flag **false** until pilot sign-off. Do not create production credits during qualification.

## Celery

Register periodic task (daily recommended):

- `users.wallet_credit_facility_v2_overdue_and_reminders`

Reminder spacing uses `WalletCreditPolicy.reminder_days_before_due` and
`overdue_reminder_interval_days` (not hardcoded aggression).

## Reconciliation (read-only)

```bash
python manage.py reconcile_wallet_credit --output docs/release/wallet_credit_reconciliation.json
```

Or API: `GET /api/admin/wallet-credit/reconcile/` (Main Admin / Finance).

## Rollback

1. Set `WALLET_CREDIT_FACILITY_V2_ENABLED=false` and policy `enabled=False`.
2. Do **not** reverse migration on production if any facilities were posted.
3. Historical SubWallet CREDIT/DEBIT rows and credit facility tables remain for audit.
4. Retired automatic overdraft remains disabled (intentional).

## Separation of duties

| Role | Capabilities |
|------|----------------|
| User | request, view own, repay, invoice PDF |
| Main Admin | approve/reduce/reject/clarification/post credit/policy |
| Finance (Accounts) | view, post credit, reconcile |
| Dept Admin | view department requests (no approve) |
| Student | always denied at API |

## Production safety checklist

- [ ] No production financial writes during this rollout task
- [ ] Feature flag false in production until pilot
- [ ] Migration applied only after staging verification
- [ ] Student API still returns 403 when flag is on
