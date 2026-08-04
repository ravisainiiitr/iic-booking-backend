# Security Test Plan — Phase 2.5

**Date:** 2026-08-04  
**Scope:** Authentication, authorization, pairing, secrets handling, config integrity, and audit for Phase 1 Plug-and-Play + Phase 2 Enterprise Lifecycle.

Security SAT for Remote Analysis remains in [`docs/sat/`](../sat/) (SAT-07). This plan extends coverage to DSA, Wizard, Deployment Center, and Lab Infrastructure.

---

## 1. Security objectives

1. Verify fail-closed behavior when secrets or keys are unset (H-01, H-04).
2. Confirm role boundaries for Main Admin vs all other personas (SAT-SEC-002).
3. Validate agent authentication and update/report endpoints (H-07, H-08).
4. Ensure no plaintext OTP or credentials persist after validation (H-02).
5. Confirm configuration integrity (HMAC signatures, ack auth).
6. Produce evidence for production readiness sign-off.

**Deferred (document only, not blocking Phase 2.5 SAT):** mTLS agent transport, SMS/WhatsApp secret channels, temp sensor integrations.

---

## 2. Threat model summary

| Asset | Threat | Control |
|-------|--------|---------|
| Pairing tokens | Unauthorized EqPC registration | ManagementApiKey required; TTL; fail-closed |
| DSA local API | Remote status forgery | Pairing token or mgmt key; loopback auth (H-04) |
| Agent tokens | Token replay / theft | Enrollment key; rotate; reject invalid |
| Config packs | Tampering in transit | HMAC-SHA256 signature on bootstrap |
| Lab APIs | Privilege escalation | Main Admin only for fleet/deployment/test dashboard |
| Guacamole URLs | Session hijack | Short-lived tokens; booking-scoped auth |
| Installers | Supply-chain tampering | SHA-256 in Deployment Center; signed tickets |

---

## 3. Authorization matrix

| Resource / action | Main Admin | Dept Admin | Faculty | External | Operator | Agent (DSA) | Agent (RAA) |
|-------------------|:----------:|:----------:|:-------:|:--------:|:--------:|:-----------:|:-----------:|
| `/deployment-center` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `/laboratory-infrastructure` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `/test-dashboard` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `/api/v1/lab/infrastructure/` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `/api/v1/lab/infrastructure/nodes/{id}/repair/` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| `/api/v1/lab/configuration/ack/` | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (token) | ✗ |
| `/api/v1/sync/agents/heartbeat/` | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (token) | ✗ |
| `/api/v1/analysis/agents/heartbeat/` | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (token) |
| `/api/v1/analysis/updates/discover/` | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (enrollment/agent) |
| `/api/v1/analysis/updates/report/` | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (agent) |
| DSA `POST /api/pairing/issue` | ✗* | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Booking create / RA session | ✓ | ✓** | ✓ | ✓** | ✓** | ✗ | ✗ |

\* Requires DSA `ManagementApiKey` on server; not a Portal role.  
\** Scope limited to department/org entitlements.

---

## 4. Security test cases

| Test ID | Category | Test steps | Expected result | SAT ref | Status |
|---------|----------|------------|-----------------|---------|--------|
| SEC-01 | Pairing | Unset ManagementApiKey; POST pairing issue | 403 Forbidden | SAT-SEC-001 / H-01 | |
| SEC-02 | Pairing | Expired pairing token on announce | 401/403 | SAT-COM-003 | |
| SEC-03 | Pairing | Valid token from wrong subnet (if restricted) | Denied or policy-bound | — | |
| SEC-04 | Loopback | Status ingest without token from non-loopback | 401/403 | SAT-SEC-004 / H-04 | |
| SEC-05 | Loopback | Status with valid pairing token | 200 | SAT-SEC-004 | |
| SEC-06 | OTP storage | Complete wizard validate; inspect DSA ConfigJson | No OTP field persisted | H-02 | |
| SEC-07 | RBAC | Faculty GET lab infrastructure API | 403 | SAT-SEC-002 | |
| SEC-08 | RBAC | Anonymous GET test dashboard | Redirect/login | SAT-SEC-002 | |
| SEC-09 | Agent auth | Heartbeat with revoked token | 401 | SAT-SEC-003 | |
| SEC-10 | Agent auth | Replay old bootstrap signature | Rejected | SAT-SEC-003 | |
| SEC-11 | Updates | Discover without enrollment key | 401/403 | H-07 | |
| SEC-12 | Updates | Report without agent token | 401/403 | H-08 | |
| SEC-13 | Updates | Valid agent report | 201; visible in fleet | H-07/H-08 | |
| SEC-14 | Config ack | Ack without DSA token | 401 | SAT-API-001 | |
| SEC-15 | Config ack | Ack with wrong version | 400; no false Applied | SAT-API-002 | |
| SEC-16 | Installers | Tampered installer bytes vs SHA-256 | Mismatch detected before run | SAT-DEP-004 | |
| SEC-17 | Secrets on disk | Inspect DSA/RAA ProgramData | No plaintext passwords | SAT-SEC-003 | |
| SEC-18 | Guacamole | Share session URL across users | Second user denied | SAT-RA-001 | |
| SEC-19 | CSRF | State-changing lab POST without CSRF (session auth) | 403 | SAT-SEC-002 | |
| SEC-20 | Audit | Repair + config push + failed pairing | Audit rows with actor/time | SAT-FLT-003 | |

---

## 5. Pairing and DSA local API

### 5.1 Fail-closed (H-01 — Resolved)

When `ManagementApiKey` is empty or unset:

- `POST /api/pairing/issue` MUST return **403**.
- Wizard MUST display actionable error (configure key on DSA).

**Evidence:** HTTP trace + DSA config screenshot (redacted key presence only).

### 5.2 Loopback status ingest (H-04 — Resolved)

- Unauthenticated status posts from non-loopback addresses MUST fail.
- Authenticated paths: valid pairing token OR management key header.

---

## 6. Secrets and credential storage

| Location | Allowed | Prohibited |
|----------|---------|------------|
| Windows Credential Manager | Service account passwords (intent) | — |
| DSA SQLite ConfigJson | Policy metadata, non-secret config | OTP after validation (H-02) |
| RAA ProgramData | Encrypted/token stores per agent design | Plaintext enrollment secrets in logs |
| Portal DB | Hashed passwords, agent token hashes | Plaintext OTP |
| Deployment Center | SHA-256 checksums, public release notes | Embedded private keys |

**Verification procedure (SEC-17):**

1. Complete wizard pairing on test EqPC.
2. Search `%ProgramData%` and DSA DB for OTP strings and password literals.
3. Confirm logs redact tokens at INFO level.

---

## 7. Configuration integrity

| Control | Verification |
|---------|--------------|
| `configuration_signature` | Bootstrap JSON includes HMAC-SHA256; tamper one field → DSA rejects or warns |
| Version monotonicity | Rollback increments version; ack matches active version |
| Idempotent ack | Duplicate ack does not create conflicting Applied states |

Reference: [`docs/enterprise/ConfigurationPush.md`](../enterprise/ConfigurationPush.md).

---

## 8. Audit requirements

Events that MUST appear in unified lab audit (`/api/v1/lab/audit/`) or domain audit tables:

| Event | Minimum fields |
|-------|----------------|
| Configuration push | profile id, version, actor |
| Configuration rollback | from/to version, actor |
| Repair action | node id, action type, actor |
| Failed pairing | source IP, reason |
| Agent registration | agent id, fingerprint |
| RA session lifecycle | booking id, workstation, user |

**Guacamole session recording:** N/A if not implemented — document as known limitation; do not fail SEC suite on missing feature.

---

## 9. Penetration / negative testing checklist

- [ ] SQL injection on lab API query params (id filters)
- [ ] IDOR on `/nodes/{id}/` with sequential IDs as non-admin
- [ ] Mass assignment on config profile POST
- [ ] Rate limit or graceful degradation on heartbeat flood
- [ ] Path traversal in sync file names (RA workspace)
- [ ] Open redirect on login next parameter

Record findings with severity mapping to C/H/M/L IDs in Production Readiness Report.

---

## 10. Entry / exit criteria

**Entry:** H-01, H-02, H-04, H-07, H-08 fixes deployed; staging agents enrolled.

**Exit:**

- [ ] SEC-01 through SEC-20 executed; no Critical failures
- [ ] Authorization matrix spot-checked for all personas
- [ ] Security reviewer sign-off below

---

## 11. Sign-off

| Role | Name | Date | Result |
|------|------|------|--------|
| Security reviewer | | | |
| Portal engineering | | | |
| SAT lead | | | |
