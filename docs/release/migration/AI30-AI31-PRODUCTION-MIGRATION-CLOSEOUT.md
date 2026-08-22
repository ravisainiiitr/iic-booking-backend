# Production Migration Closeout

**Updated (UTC):** 2026-08-21T17:36Z  
**Verdict:** **PRODUCTION CLOSEOUT = PASS**

```text
MIGRATE_EXECUTED (prior) = YES
THIS_PHASE_MIGRATE = NO
OPERATOR_CHANNEL_I_LOGIN = DONE (normal browser flow)
ChannelIIdentityProfile_rows = 1
employee_match_count = 1
MANUAL_PROFILE_INSERT = NO
```

---

## Production baseline

| Field | Value |
|-------|--------|
| Production SHA | `7d1081da39e3b0c0d44e58ab0e4172ec217c46ea` |
| Production tag | `v2.5.2-channel-i-user-savepoint` |
| SHA unchanged | **YES** |
| Migration workflow | GitHub Actions **Migrate Production** run **32506064057** |
| Migration start (UTC) | 2026-08-21T17:02:34Z |
| Migration end (UTC) | ~2026-08-21T17:03:44Z |
| Migration result | **PASS** |
| Backup reference | `/home/ubuntu/backups/nightly/nightly-20260821/db/portal.sql.gz` (`gzip_ok=YES`) |

---

## Applied migrations

| Migration | Result |
|-----------|--------|
| users.0096 | **[X] APPLIED** |
| users.0097 | **[X] APPLIED** |
| users.0098 | **[X] APPLIED** |
| users.0099 | **[X] APPLIED** |
| users.0100 | **[X] APPLIED** |
| Unexpected | **NONE** |
| Pending | **NONE** |
| R14 | **NOT APPLIED** |
| equipment.0188 | **NOT APPLIED** |

---

## Channel-I (post-login)

| Check | Result |
|-------|--------|
| Authorize | **PASS** (`channeli.in`, REAL) |
| Callback | **PASS** (route healthy; login completed by operator) |
| Live userinfo | **PASS** (evidenced by successful profile sync) |
| Fixture fallback | **NONE** (`fixture_mode=False`) |
| Evidence class | **REAL** |
| Authoritative claim | **username** (unchanged) |
| Employee match count | **1** (Channel-I username ↔ `admin.users.emp_id`; ID not printed) |
| ChannelIIdentityProfile | **PRESENT AND POPULATED** (rows=**1**) |
| Durable identity | **PASS** |
| Wallet identity | **PASS** (exact match=1; legacy wallet rows for match=1) |

---

## MySQL RO / data planes

| Check | Result |
|-------|--------|
| Host / port / db / user | `host.docker.internal` / `3306` / `admin` / `iic_booking_ro` |
| users | **PASS** |
| user_wallet (wallet) | **PASS** |
| wallet_transactions (ledger) | **PASS** |
| booking | **PASS** |
| `account_appears_writable` | **FALSE** |
| MySQL RO overall | **PASS** |

---

## Health / S3 / backups

| Check | Result |
|-------|--------|
| Django readiness | **PASS** (200) |
| Django container | healthy |
| Frontend | healthy |
| Celery | Up |
| Redis | healthy |
| S3 | **PASS** |
| Backup | **PASS** |

---

## Production write audit (this closeout phase)

| Activity | Performed? |
|----------|------------|
| migrate / makemigrations | **NO** |
| Schema modification | **NO** |
| MySQL grant/credential change | **NO** |
| Test wallet / ledger / booking writes | **NO** |
| Manual `ChannelIIdentityProfile` insert | **NO** |
| Redeploy / SHA change | **NO** |
| Normal Channel-I login profile sync | **YES** (application behavior; separate from migration write safety) |

---

## Final GO / NO-GO

| Criterion | Status |
|-----------|--------|
| Production health | **PASS** |
| 0096–0100 applied | **PASS** |
| No unexpected migrations | **PASS** |
| Channel-I REAL | **PASS** |
| Live userinfo | **PASS** |
| Claim = username | **PASS** |
| Employee match = 1 | **PASS** |
| ChannelIIdentityProfile present + populated | **PASS** |
| MySQL RO | **PASS** |
| Wallet / ledger / booking | **PASS** |
| S3 / backups | **PASS** |
| R14/0188 not applied | **PASS** |
| No unauthorized production writes | **PASS** |
| **PRODUCTION CLOSEOUT** | **PASS** |

Application rollback ≠ database rollback. DB recovery remains the nightly backup above + documented restore procedures.
