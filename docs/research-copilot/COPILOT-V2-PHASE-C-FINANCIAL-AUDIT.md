# Copilot V2 Phase C — Financial System Audit

**Date:** 2026-08-26  
**Scope:** Reuse existing portal wallet / recharge / credit / invoice domain services.  
**Migrations for Phase C:** **None required** (Credit Facility V2 + payments already shipped).

## Principle

Research Copilot is an **orchestrator**. Money movement stays in:

- `SubWallet.credit` / `SubWallet.debit`
- `payments` Razorpay order + webhook settle
- `wallet_credit_facility_v2` request / approve / post / repay

Never: LLM → direct DB balance writes.

---

## Existing domain inventory (reuse)

### Wallet ledger
| Piece | Location |
|------|----------|
| `Wallet`, `SubWallet`, `SubWalletTransaction` | `users/models/wallet.py` |
| Balance | `Wallet.total_balance` = sum of sub-wallets |
| User APIs | `GET /api/wallet/`, `/wallet/balance/`, `/wallet/transactions/` |

### Online recharge (preferred)
| Piece | Location |
|------|----------|
| Create order | `POST /api/payments/razorpay/create-order/` (`purpose=WALLET_RECHARGE`) |
| Verify / webhook | payments Razorpay verify + webhook |
| Settlement | `_settle_wallet_recharge` → `SubWallet.credit` |

Copilot may **prepare** an order and hand off Checkout. It must **not** mark payment success from user chat.

### Offline / SRIC recharge
OTP + approve workflow (`wallet_recharge_workflow.py`). Prefer deep-link `/wallet` for heavy UX in Phase C v1.

### Wallet Credit Facility V2
| Piece | Location |
|------|----------|
| Service | `users/wallet_credit_facility_v2.py` |
| APIs | `/api/wallet/credit-requests/`, summary, repay, invoice.pdf |
| Admin approve/reduce/reject | `/api/admin/wallet-credit/<id>/…` (Main Admin) |
| FE | `/wallet/credit-facility`, `/admin/wallet-credit` |

Copilot may create a **pending** request after confirmation. Copilot **never** approves credit.

### Invoices / receipts
- Credit invoice PDF: existing credit facility endpoint  
- Booking invoice: `/api/bookings/<id>/invoice.pdf`  
- Wallet “statement”: transaction list + FE export (no fabricated LLM invoices)

---

## Copilot gaps addressed in Phase C implementation

1. Fix `_get_wallet` to use `total_balance` (was broken `wallet.balance`)
2. Domain bridge for wallet/credit/recharge reads + gated executes
3. Deterministic financial intents (balance, txs, spend, credit status, estimate+coverage)
4. Recharge / credit **proposals** with confirmation + idempotency (flags OFF)
5. Booking prepare offers recharge/credit actions when insufficient
6. FE proposal cards for recharge/credit
7. Feature flags for reads vs mutations

---

## Feature flags

| Flag | Default | Purpose |
|------|---------|---------|
| `COPILOT_WALLET_READ` | true | Deterministic wallet/credit reads |
| `COPILOT_INVOICE_READ` | true | Link to authoritative invoice/receipt URLs |
| `COPILOT_FINANCIAL_PROPOSALS` | false | Allow prepare cards for recharge/credit |
| `COPILOT_WALLET_RECHARGE` | false | Execute Razorpay order create via Copilot |
| `COPILOT_WALLET_CREDIT` | false | Execute credit request submit via Copilot |
| `COPILOT_FINANCIAL_ADMIN` | false | Copilot admin financial dashboards (use portal admin UI) |
| `WALLET_CREDIT_FACILITY_V2_ENABLED` / `WALLET_CREDIT_ENABLED` | env | Domain credit facility master switches |

---

## Security

- Authenticated Django user only; strip LLM `user_id`
- Cross-user wallet/credit/invoice access denied by existing APIs
- Soft “okay” never confirms financial mutations
- Idempotency keys on execute
- Audit without secrets / gateway keys

---

## Enablement posture

Ship code with **all financial mutation flags OFF**.  
Controlled financial E2E (test account + payment/credit path) required before `READY FOR CONTROLLED FINANCIAL ENABLEMENT`.
