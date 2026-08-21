# AI30/AI31 — REAL Integration Operator Handoff

**Authoritative document for the operator who holds external credentials.**  
**Do not put secrets in this file. Do not commit `.envs/.staging/.django`.**

---

## Freeze status

| Layer | Status |
|-------|--------|
| **CODE-SIDE REAL INTEGRATION TOOLING** | **READY** |
| **EXTERNAL CONFIGURATION** | **BLOCKED** |
| **REAL STAGING READ-ONLY INTEGRATION** | **NOT VERIFIED** |
| **PRODUCTION** | **UNCHANGED** |
| **PRODUCTION WRITES** | **NO** |

Implementation is **frozen** until live staging credentials are supplied.  
Future code changes should be driven only by a **real** staging failure after configuration exists — not by inventing credentials or weakening guards.

---

## Current verified environment

| Item | Value |
|------|-------|
| Settings | `config.settings.staging` |
| Database | `iic_booking_staging` @ `postgres` |
| Backend commit | `f7783f9` |
| Frontend commit | `de71188` |
| Automated guards | **25 PASS** |
| Overall | **BLOCKED — CASE A** |

---

## Operator configuration table

Presence/state only — **never** paste secret values here.

| Variable | Required value/state | Source/Action | Status |
|----------|----------------------|---------------|--------|
| `OMNIPORT_CLIENT_ID` | Real **staging** OAuth client ID | Omniport administrator | **BLOCKED** |
| `OMNIPORT_CLIENT_SECRET` | Real **staging** OAuth secret | Omniport administrator | **BLOCKED** |
| `OMNIPORT_REDIRECT_URI` | Must end with `/api/auth/omniport/callback/` | Register with Omniport staging app | **INVALID** (still legacy `/api/v1/auth/channel-i/callback/`) |
| `OLD_MYSQL_HOST` | Staging-approved legacy MySQL host | DB administrator | **BLOCKED** |
| `OLD_MYSQL_PORT` | Approved MySQL port (usually 3306) | DB administrator | default only / insufficient alone |
| `OLD_MYSQL_DATABASE` | Legacy database name | DB administrator | **BLOCKED** |
| `OLD_MYSQL_USER` | **Read-only** DB user | DB administrator | **BLOCKED** |
| `OLD_MYSQL_PASSWORD` | Read-only DB password | DB administrator | **BLOCKED** |
| `CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM` | Proven claim path only | From real Omniport identity response | **BLOCKED** |
| `REAL_INTEGRATION_ENABLED` | `true` | Operator (manual) | **FALSE** |
| Fixture modes | `CHANNEL_I_STAGING_FIXTURE_MODE=false`, `LEGACY_MYSQL_STAGING_FIXTURE_MODE=false` | Operator | not yet set for REAL |
| `STAGING_STORAGE_BACKEND` | `S3` if real S3 required | Operator | **LOCAL_STAGING** |
| `USE_S3_MEDIA` | `true` if S3 required | Operator | `False` / **NOT CONFIGURED** for REAL S3 |

Allowed employee claim values (do not invent others; do not use email/name as silent default):

- `operator_confirmed_map`
- `username`
- `student.enrolmentNumber`
- `facultyMember.employeeId`

---

## Exact operator steps

### STEP 1 — Populate staging env (gitignored)

Copy template → local secrets file:

- Template: `docs/release/migration/sample.env.staging`
- Target: `.envs/.staging/.django` (**never commit**)

Fill in real staging values. Commands **will not** edit this file for you.

### STEP 2 — Register Omniport callback

Register **exactly**:

```text
/api/auth/omniport/callback/
```

Example for local Docker staging:

```text
http://127.0.0.1:8180/api/auth/omniport/callback/
```

**Reject** legacy:

```text
/api/v1/auth/channel-i/callback/
```

Activation tooling reports `CHANNEL-I REDIRECT URI INVALID` if the legacy path remains.

### STEP 3 — Prove employee ID claim

After a **real** staging Omniport identity response is available, set:

`CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM=<proven path>`

Do **not** guess. Do **not** substitute email/display name unless that path is explicitly the configured claim (it is not a default).

### STEP 4 — Provide read-only legacy MySQL

Set `OLD_MYSQL_HOST`, `OLD_MYSQL_PORT`, `OLD_MYSQL_DATABASE`, `OLD_MYSQL_USER`, `OLD_MYSQL_PASSWORD`.

Account must be **READ-ONLY** (no INSERT/UPDATE/DELETE/ALTER/DROP/TRUNCATE).

### STEP 5 — S3 decision

- **If required:** `STAGING_STORAGE_BACKEND=S3`, `USE_S3_MEDIA=True`, staging-isolated AWS only.  
- **If not:** formally retain `LOCAL_STAGING` → set `LOCAL_STAGING_ACCEPTED=true` → evidence status **NOT_AVAILABLE / ACCEPTED LIMITATION** (not a PASS).

### STEP 6 — Enable REAL mode flags (manual)

```text
REAL_INTEGRATION_ENABLED=true
CHANNEL_I_STAGING_FIXTURE_MODE=false
LEGACY_MYSQL_STAGING_FIXTURE_MODE=false
```

### STEP 7 — Restart / recreate **staging** services only

Docker Compose loads `.envs/.staging/.django` via `env_file` at **container create** time.

**`docker restart` is not enough** after editing the env file. Recreate:

```bash
docker compose -f docker-compose.staging.yml --env-file .envs/.staging/.django \
  up -d --force-recreate --no-deps django celeryworker celerybeat
```

Do not restart or touch production.

For LOCAL staging legacy MySQL, keep the Windows SSH tunnel listening on `127.0.0.1:13306` while probing (`OLD_MYSQL_HOST=host.docker.internal`).

### STEP 8 — Status

```bash
docker exec iic-booking-staging-django python manage.py real_integration_status
# or: python manage.py real_integration_status
```

### STEP 9 — Preflight

```bash
docker exec iic-booking-staging-django python manage.py real_integration_preflight \
  --json --write-docs \
  --backend-commit f7783f9 \
  --frontend-commit de71188
```

Credential **presence alone is not PASS**.

### STEP 10 — Activate / live read-only probes

```bash
docker exec iic-booking-staging-django python manage.py real_integration_activate_staging \
  --write-docs \
  --backend-commit f7783f9 \
  --frontend-commit de71188
```

This command is fail-closed. It does **not** edit `.env`, invent credentials, or touch production.

Related checklist: `docs/release/migration/REAL_INTEGRATION_CREDENTIAL_CHECKLIST.md`

---

## DO NOT

- use production Omniport credentials for staging REAL activation  
- use production MySQL credentials  
- use production S3 / production prefixes  
- edit production environment variables  
- SSH to production for this stage  
- run production migrations  
- write to production RDS  
- modify legacy wallet balances  
- modify legacy bookings  
- create fake employee IDs  
- enable fixture fallback while REAL mode is on  
- commit `.env` / `.envs/.staging/.django`  
- put secrets into documentation or source code  
- claim REAL integration PASS from config presence alone  
- proceed to production migration without separate approval  

---

## Success criteria

**REAL STAGING READ-ONLY INTEGRATION VERIFIED** only when **all** of the following are true (live, not simulated):

- [ ] Channel-I real staging OAuth succeeds  
- [ ] Correct callback `/api/auth/omniport/callback/` verified  
- [ ] Employee ID claim proven from real identity response  
- [ ] Employee maps to IIC Booking identity  
- [ ] Real legacy MySQL connection succeeds  
- [ ] MySQL access is read-only  
- [ ] Wallet reads succeed  
- [ ] Booking reads succeed  
- [ ] Fixture isolation remains PASS  
- [ ] S3 is PASS **or** formally documented NOT_AVAILABLE  
- [ ] Production writes = NO  
- [ ] Evidence generated under `docs/release/migration/`  

Financial writes (deduction / recharge / transfer / ledger write) are **out of scope** for this phase.

---

## Failure behavior

| Condition | Result |
|-----------|--------|
| Credentials missing | **NOT READY — OPERATOR CONFIGURATION REQUIRED** |
| Credentials exist but live OAuth fails | **Channel-I = FAILED/BLOCKED** |
| MySQL authentication fails | **Legacy MySQL = FAILED/BLOCKED** |
| Employee claim missing/ambiguous | **Employee Identity = BLOCKED** (stop dependent wallet verification) |
| Fixture fallback under REAL mode | **CRITICAL FAILURE** |
| Any production resource touched | **CRITICAL FAILURE — STOP** |

---

## Code-side tooling inventory (frozen)

| Component | Role |
|-----------|------|
| `real_integration_guards.py` | Presence checks, redirect validation, claim allow-list, preflight/status builders |
| `real_integration_activation.py` | Fail-closed activation orchestration; never edits env |
| `real_integration_status` | Lightweight operator status |
| `real_integration_preflight` | Deterministic preflight + evidence JSON |
| `real_integration_activate_staging` | Controlled activation / live probes |
| `snapshot_reader.get_legacy_reader` | Refuses fixture when REAL intent |
| `channel_i_fixture.py` | Blocked when `REAL_INTEGRATION_ENABLED=true` |
| `OldMySQLReader` + `assert_readonly_sql` | Read-only legacy MySQL path |
| Tests (`test_real_integration_*`) | **25 PASS** without real credentials |

---

## After credentials are supplied

Re-run the activation prompt / commands above.  
Target outcome: **REAL STAGING READ-ONLY INTEGRATION VERIFIED**.  

Then — and only then — a separate **PRODUCTION MIGRATION PRE-FLIGHT** may be considered under explicit approval.

**Until then: do not proceed to production migration.**
