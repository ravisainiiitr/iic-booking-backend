# Omniport callback success + frontend "Failed to fetch" (STAGING)

**Date:** 2026-08-21  
**Scope:** Local Docker staging only. Production untouched.  
**REAL_INTEGRATION_ENABLED:** remains `false`

## Observation

Browser completed Omniport login and returned to:

`http://127.0.0.1:8100/auth/callback`

with application-generated query params (`token`, `user_id`, `email`, `name`, `user_type`).  
Frontend UI: **Authentication Failed / Failed to fetch**.

Token values were **not** recorded.

## Backend callback result (A–C)

From staging Django logs around the attempt:

| Step | Result |
|------|--------|
| Omniport authorize | PASS (prior + post-fix) |
| `/api/auth/omniport/callback/` received `code`+`state` | PASS |
| OAuth token exchange | PASS (`access_token`, `refresh_token`, `expires_in`, …) |
| Channel-I userinfo retrieval | PASS |
| App token issued + redirect to frontend `/auth/callback` | PASS |

**Conclusion:** Real OAuth callback **completed successfully**. Failure was **after** redirect.

### Userinfo key summary (no PII / no token values)

Top-level keys observed:

`biologicalInformation`, `contactInformation`, `facultyMember`, `person`, `student`, `userId`, `username`

Nested (types/keys only):

- `userId`: int  
- `username`: str  
- `person`: `displayPicture`, `fullName`  
- `student`: empty  
- `facultyMember`: empty  
- `contactInformation`: email/phone related keys present  

Note: empty `student` / `facultyMember` → role defaulted to faculty in callback logs.

## Where the frontend failed

`AuthCallback.tsx` on `token` query param:

1. `apiClient.setToken(token)`
2. `apiClient.getCurrentUser()` → `GET {VITE_API_URL}/auth/user/`  
   Runtime config: `http://127.0.0.1:8180/api`

Browser then reported **Failed to fetch** because staging Django CORS allowed only:

`http://localhost:8080`, `http://127.0.0.1:8080`

Frontend runs on **`:8100`**. OPTIONS/GET from `Origin: http://127.0.0.1:8100` had empty `Access-Control-Allow-Origin`.

### Secondary UX issue (not the fetch error)

First-login welcome email used SMTP with `fail_silently=False` and delayed redirect ~30s before frontend load. Softened for Omniport path (`fail_silently=True` + no email in exception log). Redirect logging no longer includes token/query string.

## Authoritative employee-ID claim (D–F)

| Check | Result |
|-------|--------|
| `facultyMember.employeeId` in live payload | ABSENT (empty facultyMember) |
| `student.enrolmentNumber` in live payload | ABSENT (empty student) |
| Channel-I claim present | **`username`** (stored on `ChannelIIdentityProfile.channel_i_username`) |
| RO match `admin.users.emp_id = <username>` via `OldMySQLReader` | **PASS** (exact match count = 1) |
| Fixture used | NO |
| Legacy DB modified | NO |

**Authoritative claim NAME (not value):** `username`

Staging env now sets:

`CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM=username`

(for staff/empty facultyMember case proven live; students still documented as `student.enrolmentNumber` / dual `operator_confirmed_map` for later review).

## Fixes applied (staging)

1. `config/settings/staging.py` — default `CORS_ALLOWED_ORIGINS` includes `:8100`  
2. `.envs/.staging/.django` — `DJANGO_CORS_ALLOWED_ORIGINS=…8100…` + claim=`username`  
3. `docs/release/migration/sample.env.staging` — document CORS + proven claim  
4. `auth_views.py` — redact redirect logs; do not block OAuth redirect on welcome-mail SMTP  

Containers recreated: `django`, `celeryworker`, `celerybeat` (image rebuilt).

Post-fix CORS OPTIONS from `http://127.0.0.1:8100` → `Access-Control-Allow-Origin: http://127.0.0.1:8100`.

## Gate summary

| Gate | Status |
|------|--------|
| A. Real Omniport authorization | PASS |
| B. Real OAuth callback | PASS |
| C. Real Omniport userinfo | PASS |
| D. Authoritative employee-ID claim | VERIFIED = `username` |
| E. Matches `admin.users.emp_id` | PASS |
| F. Claim configured safely | YES (`username`) |
| G. `REAL_INTEGRATION_ENABLED` | **false** |
| H. Production | untouched |

`real_integration_status`: Channel-I READY, Redirect VALID, Legacy MySQL READY, Employee Identity READY, Overall NOT READY (REAL mode not enabled — expected).

Guard tests: **25 OK**.

## Operator next step

Re-try Omniport login in the browser against staging. After redirect, `/auth/user/` should succeed (CORS fixed). Do not paste tokens into chat.
