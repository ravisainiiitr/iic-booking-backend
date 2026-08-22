# Production Migration GO/NO-GO

**Updated (UTC):** 2026-08-21T17:36Z  
**Overall:** **PRODUCTION MIGRATION = PASS** · **PRODUCTION CLOSEOUT = PASS**

```text
MIGRATE_EXECUTED = YES
OPERATOR_CONFIRMATION = MIGRATE
WORKFLOW = Migrate Production (run 32506064057)
OPERATOR_CHANNEL_I_LOGIN = DONE
ChannelIIdentityProfile_rows = 1
employee_match_count = 1
PRODUCTION WRITES (test/manual) = NO
CLOSEOUT_DOC = AI30-AI31-PRODUCTION-MIGRATION-CLOSEOUT.md
```

---

## Timing

| Event | UTC |
|-------|-----|
| Operator approval (`MIGRATE`) | 2026-08-21 (~17:02) |
| Migration start | 2026-08-21T17:02:34Z |
| Migration completion | 2026-08-21T17:03:44Z (± workflow end) |
| Backup used | **2026-08-21 02:30** — `nightly-20260821/db/portal.sql.gz` |
| Post-login closeout verify | run **32508928935** |

---

## Production release

| Field | Value |
|-------|--------|
| SHA | `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea` |
| Tag | `v2.5.2-channel-i-user-savepoint` |
| Unchanged through closeout | **YES** |

---

## Applied migrations

| Migration | Result |
|-----------|--------|
| `users.0096` … `users.0100` | all **[X] OK** |
| Unexpected | **NONE** |
| `equipment.0188` / R14 | **NOT APPLIED** |

---

## Channel-I + durable identity (closeout)

| Check | Result |
|-------|--------|
| Authorize / callback / live userinfo | **PASS** (REAL) |
| Fixture | **NONE** |
| Claim | **username** |
| Employee match | **1** |
| ChannelIIdentityProfile | **PASS** (rows=1, populated via normal login) |
| Wallet identity | **PASS** |

---

## Integrations / health

MySQL RO, wallet, ledger, booking, S3, backups, Django/frontend/Celery/Redis: all **PASS**.

---

## Final

**PRODUCTION CLOSEOUT = PASS**

See `AI30-AI31-PRODUCTION-MIGRATION-CLOSEOUT.md` for the full closeout report.
