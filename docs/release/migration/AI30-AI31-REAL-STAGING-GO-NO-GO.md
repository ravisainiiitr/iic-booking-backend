# REAL Staging — Final Qualification GO/NO-GO

**Timestamp (UTC):** 2026-08-21 (activation run after evidence reconciliation)  
**Environment:** STAGING (local Docker)  
**REAL_INTEGRATION_ENABLED:** `true`  
**Production EC2 / RDS / writes:** untouched / NO

## Verdict

# **GO** — READY FOR REAL STAGING INTEGRATION

(with explicit S3 limitation: `NOT_AVAILABLE / ACCEPTED LIMITATION`)

## Gate results

| Gate | Status | Evidence class |
|------|--------|----------------|
| Channel-I live OAuth / userinfo | **PASS** | **REAL** |
| Employee Identity (`username`) | **PASS** | **REAL** (exact `admin.users.emp_id` match count = 1) |
| Legacy MySQL RO | **PASS** | **REAL** |
| Wallet read | **PASS** | REAL |
| Ledger read | **PASS** | REAL |
| Booking read | **PASS** | REAL |
| Fixture Isolation | **PASS** | — |
| Guard tests (full suite) | **30 PASS** | — |
| Staging S3 | **NOT_AVAILABLE / ACCEPTED LIMITATION** | LOCAL_STAGING (`LOCAL_STAGING_ACCEPTED=true`) — **not** a PASS |

## How Channel-I evidence was reconciled (no fabrication)

Prior browser Omniport login already completed code exchange + userinfo and wrote a durable `ChannelIIdentityProfile`.

Tooling now **re-verifies** (without new tokens/codes):

1. Fixture modes off  
2. Redirect path valid  
3. Claim `username` configured  
4. Durable profile has Channel-I username from prior real callback  
5. Live `OldMySQLReader`: `emp_id` exact match count = 1  
6. No email/name fallback; no fixture reader  

Evidence files:

- `docs/release/migration/real_channel_i_live_evidence.json`
- `docs/release/migration/real_integration_preflight.json`
- `docs/release/migration/real_integration_activation.json`
- `docs/release/migration/AI30-AI31-OMNIPORT-CALLBACK-FRONTEND-FETCH-DEBUG.md`

## S3 policy (outcome B)

Real S3 is **not** configured for this local staging qualification (`STAGING_STORAGE_BACKEND=LOCAL_STAGING`).

Operator acceptance flag:

```text
LOCAL_STAGING_ACCEPTED=true
```

Final evidence wording:

**S3 = NOT_AVAILABLE / ACCEPTED LIMITATION**

This is **not** declared PASS. Full media/S3 REAL remains a future staging task if required.

## Remaining blockers

**None** for REAL staging activation under the accepted LOCAL_STAGING limitation.

Optional future work (non-blocking for this GO):

- Configure isolated staging S3 and live-probe it if media uploads must be REAL.

## Safety confirmations

| Check | Result |
|-------|--------|
| Production EC2 | untouched |
| Production RDS | untouched |
| Production writes | NO |
| Fixture isolation | PASS (fixtures refuse under REAL) |
| MySQL write rejection | enforced by `assert_readonly_sql` / RO account |
| Secrets/tokens in evidence | not included |

## Commands re-run

```bash
python manage.py real_integration_status
python manage.py real_integration_preflight --json --write-docs
python manage.py real_integration_activate_staging --write-docs
python manage.py test iic_booking.users.tests.test_real_integration_preflight \
  iic_booking.users.tests.test_real_integration_activation
```
