# REAL Integration Credential Checklist

**Environment:** STAGING only  
**PRODUCTION WRITES = NO** until separately approved.

> **Authoritative freeze / operator handoff:**  
> [`AI30-AI31-REAL-INTEGRATION-OPERATOR-HANDOFF.md`](./AI30-AI31-REAL-INTEGRATION-OPERATOR-HANDOFF.md)

Do **not** commit `.envs/.staging/.django`, passwords, client secrets, or AWS secret keys.

This checklist complements the handoff. Implementation is **frozen** until external configuration is supplied.


---

## Operator commands (deterministic)

```bash
# 1) Lightweight status (no env edits, no secret printing)
python manage.py real_integration_status

# 2) Configuration preflight
python manage.py real_integration_preflight
python manage.py real_integration_preflight --json --write-docs \
  --backend-commit "$(git rev-parse --short HEAD)" \
  --frontend-commit "de71188"

# 3) Controlled activation / live probes (SAFE BY DEFAULT)
#    Does NOT edit .envs/.staging/.django
#    Does NOT set REAL_INTEGRATION_ENABLED automatically
python manage.py real_integration_activate_staging \
  --write-docs \
  --backend-commit "$(git rev-parse --short HEAD)" \
  --frontend-commit "de71188"

# Optional:
#   --skip-tests
#   --skip-live-probes
#   --fail-on-blocked
```

Via Docker staging:

```bash
docker exec iic-booking-staging-django python manage.py real_integration_status
docker exec iic-booking-staging-django python manage.py real_integration_activate_staging \
  --write-docs --backend-commit f7783f9 --frontend-commit de71188
```

Secrets are never printed — only `PRESENT` / `ABSENT`.

---

## Channel-I

- [ ] Client ID supplied (`OMNIPORT_CLIENT_ID`)
- [ ] Client secret supplied (`OMNIPORT_CLIENT_SECRET`)
- [ ] Redirect URI registered with Channel-I staging OAuth app
- [ ] Redirect URI exactly ends with:

```text
/api/auth/omniport/callback/
```

Example:

```text
http://127.0.0.1:8180/api/auth/omniport/callback/
```

- [ ] OAuth staging application verified
- [ ] **Do not** use legacy path `/api/v1/auth/channel-i/callback/`

## Legacy MySQL

- [ ] Host (`OLD_MYSQL_HOST`)
- [ ] Port (`OLD_MYSQL_PORT`, usually 3306)
- [ ] Database (`OLD_MYSQL_DATABASE`)
- [ ] User (`OLD_MYSQL_USER`)
- [ ] Password (`OLD_MYSQL_PASSWORD`)
- [ ] Read-only credentials confirmed (no INSERT/UPDATE/DELETE/ALTER/DROP/TRUNCATE)

## Employee Identity

- [ ] Authoritative claim identified from a **real** staging Channel-I response
- [ ] Claim configured in `CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM`
- [ ] Claim is one of:
  - `operator_confirmed_map`
  - `username`
  - `student.enrolmentNumber`
  - `facultyMember.employeeId`
- [ ] Real staging identity response proves claim
- [ ] Do **not** invent email/name as Employee ID unless explicitly configured as the claim (they are not defaults)

## Storage

- [ ] Real staging S3 configured (`STAGING_STORAGE_BACKEND=S3`, `USE_S3_MEDIA=True`, staging-isolated AWS)

**OR**

- [ ] `LOCAL_STAGING` formally accepted as **NOT_AVAILABLE** (not a PASS)

## Runtime

- [ ] `REAL_INTEGRATION_ENABLED=true` (operator sets this manually — commands will not)
- [ ] `CHANNEL_I_STAGING_FIXTURE_MODE=false`
- [ ] `LEGACY_MYSQL_STAGING_FIXTURE_MODE=false`
- [ ] Staging services restarted (**staging only**)

## Validation

- [ ] `real_integration_status` → dependencies READY / Redirect VALID
- [ ] `real_integration_preflight` → no mandatory BLOCKED (live still required)
- [ ] `real_integration_activate_staging` → live probes
- [ ] Channel-I live probe PASS (or clear OPERATOR ACTION REQUIRED, then complete OAuth)
- [ ] Employee identity PASS (proven from real response)
- [ ] MySQL read-only PASS
- [ ] Wallet read PASS
- [ ] Booking read PASS / documented
- [ ] Fixture isolation PASS
- [ ] Evidence generated under `docs/release/migration/`

---

## Variable reference

| Variable | Purpose | Required for REAL | Staging-only notes |
|----------|---------|-------------------|--------------------|
| `REAL_INTEGRATION_ENABLED` | Live intent; disables fixture success paths | Yes (`true`) | Operator sets manually |
| `OMNIPORT_CLIENT_ID` | OAuth client | Yes | Staging OAuth app |
| `OMNIPORT_CLIENT_SECRET` | OAuth secret | Yes | Never log / never commit |
| `OMNIPORT_REDIRECT_URI` | Callback | Yes | Must match `/api/auth/omniport/callback/` |
| `OLD_MYSQL_HOST` | Legacy DB host | Yes | RO preferred |
| `OLD_MYSQL_PORT` | Port | Yes | Default 3306 |
| `OLD_MYSQL_DATABASE` | DB name | Yes | |
| `OLD_MYSQL_USER` | User | Yes | RO |
| `OLD_MYSQL_PASSWORD` | Password | Yes | Never log |
| `CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM` | Emp ID policy | Yes (proven) | Empty = BLOCKED |
| `STAGING_STORAGE_BACKEND` | LOCAL_STAGING or S3 | S3 for full REAL storage | LOCAL ≠ PASS |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_STORAGE_BUCKET_NAME` | Staging S3 | If S3 | Isolated bucket/prefix |
| `CHANNEL_I_STAGING_FIXTURE_MODE` | Fixture OAuth | Must be `false` for REAL | Tests only |
| `LEGACY_MYSQL_STAGING_FIXTURE_MODE` | Fixture MySQL | Must be `false` for REAL | Tests only |

Template file: `docs/release/migration/sample.env.staging` → copy to `.envs/.staging/.django` (gitignored).

---

## Status vocabulary

| Phrase | Meaning |
|--------|---------|
| **WAITING FOR OPERATOR CONFIGURATION** | Tooling ready; credentials/REAL flag not supplied |
| **NOT READY FOR REAL INTEGRATION** | Mandatory dependency missing or live probe incomplete |
| **READY FOR REAL STAGING INTEGRATION** | Live probes succeeded on staging (still not production) |
| **READY FOR PRODUCTION MIGRATION** | **Out of scope** — separate approval |

**PRODUCTION WRITES = NO** until separately approved.
