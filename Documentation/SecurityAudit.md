# Security Audit — Remote Analysis Platform

**Date:** 2026-07-30 (updated with Phase 3 security/cleanup follow-up)  
**Scope:** Portal RA package + Remote Analysis Agent  
**Related:** `Documentation/SecurityReviewChecklist.md`  
**Sources:** Code review + [RA security audit explore](73b74fa9-d060-4f0f-bcaf-17f5e3add860) + [Cleanup search RA codebase](c30c584c-1c82-4287-8663-89009643e008)

---

## Executive summary

Security controls for agent auth, RBAC, session tokens, workspace path isolation, and Guacamole secret handling are **implemented and test-covered**.

**Critical finding:** `POST /api/v1/analysis/register/` was `AllowAny`. **Mitigation shipped:** when `RA_AGENT_ENROLLMENT_KEY` is set, registration requires `X-Enrollment-Key`; when `DEBUG=False`, readiness fails until the key is configured and mock Guacamole is disabled.

Residual risk is **operational** (TLS edge, secrets management, app-layer rate limits, virus scanner) plus **known stubs** (SMS/push, recording) and **High** items deferred (launch/Guacamole tokens in query strings; plaintext Guacamole admin / temp password in DB).

**Recommendation for pilot:** Proceed after Production Gate + enrollment key + live Guacamole.

---

## Findings by control area

| Control | Status | Evidence / notes |
|---------|--------|------------------|
| Authentication (portal users) | **PASS** | Django/DRF + portal auth stack |
| Agent authentication | **PASS** | Bearer + `X-Agent-Id`; Django password hashers; expiry/revoke |
| Agent enrollment / register | **PASS (mitigated)** | `RA_AGENT_ENROLLMENT_KEY` + `X-Enrollment-Key`; readiness requires key when `DEBUG=False` |
| JWT handling | **N/A / portal-level** | Agent tokens are opaque hashed secrets, not JWTs |
| Token expiry | **PASS** | Agent token lifetime (90d default); launch token ~90s |
| Session replay protection | **PASS** | Launch tokens single-use + optional IP bind + hash-at-rest |
| Launch token in URL | **WARNING** | Query `?t=` / Guacamole `?token=` — Referer/log risk |
| Authorization / RBAC | **PASS** | View/Manage/Agent permissions on APIs |
| Privilege escalation | **PASS** with review | Managers can act for users (lab assist); agent cannot escalate to portal RBAC |
| API validation | **PASS** | DRF serializers on create/update paths |
| SQL injection | **PASS** | ORM; only static `SELECT 1` in health probe |
| XSS | **WARNING** | JSON APIs; residual risk is token-in-URL leakage |
| CSRF | **PASS** (with Django) | Session cookie CSRF; Bearer CSRF-resistant |
| Directory traversal | **PASS** | Workspace boundary checks |
| Path validation | **PASS** | Extension allow/block + quota |
| Rate limiting | **WARNING** | No RA-specific throttles; recommend Traefik/WAF on `/register/` + heartbeat |
| Remote command validation | **PASS** | Portal allowlisted types; agent handlers slightly broader (reboot not portal-exposed) |
| TLS enforcement | **WARNING** | `verify_tls` + `RA_GUACAMOLE_VERIFY_TLS`; RDP `ignore-cert=true` separate |
| Secrets handling | **WARNING** | RDP Fernet OK; Guacamole admin + ephemeral temp password stored plaintext in DB/metadata |
| Virus scanner | **WARNING** | NoOp only — uploads marked CLEAN |
| Configuration security | **WARNING** | Defaults favor lab/dev; readiness fails closed on mock + missing enrollment key when `DEBUG=False` |

---

## Detailed notes

### Agent enrollment (Critical — mitigated)

- Unauthenticated register remains for bootstrap **only when** `RA_AGENT_ENROLLMENT_KEY` is unset (DEBUG/local/CI).
- Production: set the same value on Portal env and Agent `EnrollmentKey`.
- Readiness (`DEBUG=False`) reports `agent_enrollment=missing_RA_AGENT_ENROLLMENT_KEY` until configured.

### Agent tokens

- Issued with `secrets.token_urlsafe(48)`, stored via `make_password`.
- Plaintext on agent disk (`agent-state.json`) — harden NTFS ACL in ops.

### Launch / session tokens

- Short-lived; hashed; single-use.
- **Residual High:** plaintext appears in launch URL query string.

### Commands

- Portal enum does not expose PC reboot/shutdown; agent still has handlers if ever delivered.

### Uploads

- Virus scanner defaults to **noop** (`get_scanner()` always returns NoOp).

### Production gate (must close)

- [ ] `DEBUG=False`
- [ ] `mock_guacamole=False` (readiness fails closed when `DEBUG=False` and mock on)
- [ ] `RA_AGENT_ENROLLMENT_KEY` set; agents configured with `EnrollmentKey`
- [ ] Strong `SECRET_KEY` / Fernet derivation
- [ ] TLS at Traefik; HSTS
- [ ] Redis/DB not public
- [ ] Edge rate limits on `/api/v1/analysis/register/` and heartbeat
- [ ] Restrict Flower `:5555` if enabled

---

## Residual risks (accepted for controlled pilot)

1. NoOp virus scanner.
2. No app-layer rate limit.
3. Session recording not a real DVR.
4. SMS/WhatsApp/Push not delivered.
5. Launch/Guacamole tokens in query strings (mitigate via short TTL + HTTPS-only Referrer-Policy at edge).
6. Guacamole admin password plaintext in settings row (prefer env-only + restrict Django admin).

---

## Verdict

**PASS with operational WARNINGs** — suitable for a **controlled five-workstation pilot** after Production Gate items (including enrollment key) are completed.
