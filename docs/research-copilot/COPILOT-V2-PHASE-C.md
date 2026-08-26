# Copilot V2 Phase C — Wallet + Financial Assistant

## Status

| Item | State |
|------|--------|
| Architecture | Orchestrator → proposal → confirm → existing domain services |
| Wallet reads | Implemented (`total_balance` fix) |
| Cost estimate + coverage | Implemented |
| Recharge prepare / execute | Implemented (execute gated) |
| Credit prepare / execute | Implemented (execute gated; never auto-approves) |
| Admin credit UI | Reuse portal `/admin/wallet-credit` |
| Migrations | **None** |
| Mutation flags | **OFF** |

## Flags

```
COPILOT_WALLET_READ=true
COPILOT_INVOICE_READ=true
COPILOT_FINANCIAL_PROPOSALS=false
COPILOT_WALLET_RECHARGE=false
COPILOT_WALLET_CREDIT=false
COPILOT_FINANCIAL_ADMIN=false
```

## Domain reuse

- Balance / txs: wallet models + tools/`get_wallet`
- Recharge execute: `payments.razorpay_create_order` (`WALLET_RECHARGE`) — Checkout + webhook settle remain authoritative
- Credit execute: `wallet_credit_v2_list_or_create` POST — status stays pending until Main Admin
- Approval / reduce / reject: **existing admin APIs only** (not Copilot)

## Confirmation

Soft phrases (`okay`, `looks good`) do not confirm.  
UI / explicit Confirm + `proposal_id` + `confirmation_token` required.  
Idempotency keys on execute.

## Booking + finance

Estimate and booking prepare surface insufficient-balance guidance and offer recharge/credit actions. No automatic credit/recharge.

## Enablement

Do **not** enable `COPILOT_WALLET_RECHARGE` / `COPILOT_WALLET_CREDIT` until controlled financial E2E on a dedicated test account (see Phase C E2E report).
