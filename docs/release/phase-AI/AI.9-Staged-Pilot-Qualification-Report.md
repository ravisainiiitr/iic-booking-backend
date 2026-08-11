# AI.9 — Staged Pilot Qualification Report

**Date:** 2026-08-11  
**Mode:** AUTO continuous execution  

## 1. AI.8 baseline

| Repo | Branch | SHA (AI.8 end / AI.9 start) |
|------|--------|-----------------------------|
| Backend | `feature/ai-copilot-android` | `d51beea` |
| Android | `master` | `7a20a81` |
| Frontend | `feature/r6-remote-analysis-software-centric` | `86cb60d` |

AI.8 report: `docs/release/phase-AI/AI.8-Production-Readiness-Report.md`

AI.8 production decision retained as starting point:

- **CORE PLATFORM:** READY FOR STAGED PILOT  
- **COPILOT:** NOT READY (keep production OFF)  
- **FCM:** BLOCKED  

AI.8 PARTIAL/BLOCKED carried into AI.9: notification deep links, live S3 E2E, staging deployment, FCM.

---

## 2. Staging environment

Inspected:

- Compose files: `docker-compose.local.yml`, `docker-compose.production.yml`, `docker-compose.test.yml`, `docker-compose.ra-production.yml`, `docker-compose.guacamole.yml`
- **No** `docker-compose.staging.yml` / staging compose
- Deploy workflows target production (`equip.iitr.ac.in`) path
- Workspace production env file `.envs/.production/.django`: **MISSING** (expected; secrets not present locally)

**STAGING = BLOCKED**

Did **not** create an uncontrolled second production system.

Local qualification continued with `docker compose -f docker-compose.local.yml` (Django up; `/api/version/` responds).

---

## 3. S3 E2E

### Code path

- `iic_booking/sync/services/results_s3.py` — keys `Results/{virtual_booking_id}/…`
- Listing/download merge via booking results service + operator `BookingResultFile`
- Completion email path remains **no result attachments** (AI.7/AI.8)

### Environment

| Setting | Local runtime (observed) |
|---------|--------------------------|
| `AWS_STORAGE_BUCKET_NAME` | empty / unset |
| Default storage | `FileSystemStorage` |
| `USE_S3_MEDIA` | not set (local default keeps FS) |

### Controlled production-bucket upload

**Not executed.** No safe staging AWS credentials and no staging environment. Uploading arbitrary test objects to production would violate AI.9 safety rules.

### Failure handling (local / unit)

Added tests `iic_booking/sync/tests/test_results_s3_ai9.py`:

- Missing bucket → upload returns `None`
- Simulated client failure → returns `None`, no success key / no `head_object`
- Missing local file → returns `None`

Results-available notify path only schedules after merged files exist (`exists and results_available_notified_at is None`). Failed S3 upload does not invent availability.

**S3 Upload / Live E2E: BLOCKED** (infra)  
**S3 helper failure handling: PASS** (unit)  
**Local FS result path (AI.8): PASS** (retained)

---

## 4. Result security

Retained from AI.8 (owner 200 / other user 403). No redesign.

Unauthorized download remains server-enforced (`_booking_results_access_denied`).

Android does not embed AWS credentials or raw bucket credentials.

**Result Security: PASS** (AI.8 evidence + unchanged auth gates)

---

## 5. Notification deep links

### Gap (AI.8)

Portal links use `?booking={virtual_booking_id}`. Android previously fell back to **My Bookings** for virtual codes.

### Fix (AI.9)

1. Backend `GET /api/notifications/` now exposes:
   - `real_booking_id` (int, from metadata)
   - `virtual_booking_id` (display id)
   - existing `link`
2. Android `NotificationDto` consumes those fields.
3. Deep-link resolver (`IicBookingAppRoot`):
   - Prefer `realBookingId` → **Booking Detail**
   - Else resolve virtual id via auth-scoped `GET bookings/?search=` exact match
   - If missing/inaccessible → My Bookings + snackbar fallback
4. Recipient scoping verified by test (other user sees empty list).

Tests: `iic_booking/communication/tests/test_notifications_deeplink.py` — **PASS**

Notification destinations for confirmed / cancelled / sample / completed / results all resolve to **Booking Detail** when metadata carries `real_booking_id` (existing booking_events / results push already store it).

**Notification Deep Links: PASS** (code + API tests; live FCM tap N/A)

---

## 6. FCM

Checked:

- No `google-services.json` in Android repo
- Google Services Gradle plugin remains commented
- `FCM_SERVER_KEY` not set in local env; settings default `""`
- `send_fcm_to_token` skips when key empty (existing test)

**FCM: BLOCKED** — do not claim push delivery PASS. Production FCM remains disabled.

In-app / CommunicationLog notification path remains usable without FCM.

---

## 7. Copilot

Production safety:

- `RESEARCH_COPILOT_ENABLED` **defaults to `False`** in `config/settings/base.py`
- Local `.envs/.local/.django` may set `True` for developer gates only (gitignored)
- Workspace `.envs/.production/.django` **absent** here — cannot accidentally deploy a stale local file as production config from this workstation without an explicit production secrets mount
- No GitHub Actions workflow variables found enabling Copilot/FCM in scanned workflow text

Without staging, Copilot was **not** enabled in production and **no artificial production Copilot test** was created.

Retained AI.8 tool auth/audit pytest coverage (re-run in AI.9 suite).

**Copilot production: NOT READY**  
**Copilot authorization / audit (code): PASS** (AI.8 + regression)  
**Copilot staging live book/cancel: BLOCKED** (no staging)

---

## 8. Android release build

Commands:

```text
.\gradlew.bat clean
.\gradlew.bat test
.\gradlew.bat assembleRelease
```

Result: **BUILD SUCCESSFUL**

Release `BuildConfig`:

- `API_BASE_URL = https://equip.iitr.ac.in/api/`
- `API_ENVIRONMENT = production`
- `DEBUG = false`

Debug remains separate (`http://10.0.2.2:8000/api/` when no override).

Hardening: emulator cleartext domains moved from `main` → `src/debug/res/xml/network_security_config.xml`.  
Release APK rescan: **`10.0.2.2` count = 0**, **`127.0.0.1` count = 0**.  
Remaining `localhost` hit is OkHttp library constant `http://localhost/`, not app API config.  
`equip.iitr.ac.in` present as expected.

No `google-services.json` / no FCM enablement in release.

Release APK: `app/build/outputs/apk/release/app-release-unsigned.apk` (unsigned — signing is an ops step).

**Android Release Build: PASS** (unsigned assemble + config verification)  
**Android Release device install sanity: PARTIAL** (APK built; controlled production pilot install not forced)

---

## 9. Backend regression

Suite executed inside local Django container:

```text
pytest
  communication/tests/test_notifications_deeplink.py
  communication/tests/test_push_device.py
  sync/tests/test_results_s3_ai9.py
  research_copilot/tests/
  equipment/tests/test_booking_completion_r7.py
```

**34 passed, 0 failed, 0 skipped** (warnings only)

Covers: notifications deep-link payload, FCM skip-without-key, S3 failure helpers, Copilot, booking completion / results-available behaviour.

---

## 10. Frontend regression

Frontend SHA unchanged: **`86cb60d`**.

AI.9 made **no frontend code changes**. Web portal booking/results/notifications/analysis paths were not intentionally modified.

Working tree contains unrelated dirty files from other workstreams — **not** included in AI.9 commits.

**Frontend Regression (AI.9 delta): PASS / N/A** (no AI.9 frontend delta)

---

## 11. Security

| Control | Status |
|---------|--------|
| Production Copilot default OFF | PASS (code default `False`) |
| Production FCM disabled without credentials | PASS |
| No invented Firebase/AWS credentials | PASS |
| No production data uploads for S3 proof | PASS |
| Result emails without attachments | PASS (retained) |
| Cross-user result access denied | PASS (AI.8) |
| Notifications recipient-scoped | PASS (new test) |
| Release APK no debug API base | PASS |
| Release cleartext emulator domains removed | PASS |
| Secrets not printed in this report | PASS |

---

## 12. Pilot workflow

**Staged pilot execution: BLOCKED** — no staging environment to deploy/checklist against.

Recommended pilot path when staging exists:

1. Deploy backend + frontend + migrations  
2. Confirm `/api/version`  
3. Controlled test accounts only  
4. USER → BOOKING → SAMPLE → OPERATOR → COMPLETION → RESULT → DOWNLOAD → NOTIFICATION  
5. Enable Copilot **staging-only** if OpenAI + flag approved  
6. Keep production Copilot/FCM OFF until independently qualified  

Local core booking lifecycle remains proven from AI.7/AI.8.

---

## 13. Remaining blockers

1. **Staging environment** (compose/deploy/secrets) — BLOCKED  
2. **Live S3 bucket E2E** in non-prod controlled bucket — BLOCKED  
3. **FCM** credentials (`google-services.json` + server key) — BLOCKED  
4. **Production Copilot enablement** — NOT READY / must stay false  
5. **Signed Play/store release** — ops (current artifact unsigned)  
6. **Release APK install against production** — only with controlled pilot accounts (not executed here)

---

## 14. Production recommendation

| Area | Recommendation |
|------|----------------|
| **CORE PLATFORM** | **READY FOR LIMITED PRODUCTION PILOT** (controlled accounts; Copilot/FCM OFF) |
| **S3** | **PARTIAL** — code + failure handling PASS; live bucket E2E BLOCKED |
| **NOTIFICATIONS (in-app + deep links)** | **PASS** for deep-link plumbing; FCM still BLOCKED |
| **COPILOT** | **NOT READY** for production — keep `RESEARCH_COPILOT_ENABLED=false` |
| **FCM** | **BLOCKED** |
| **ANDROID RELEASE** | **READY** for unsigned release config qualification; signing/pilot install remain ops |
| **STAGED PILOT ENV** | **BLOCKED** until staging exists |

### Final decision: **READY FOR LIMITED PRODUCTION PILOT**

Scope: core booking / sample / completion / results / auth / in-app notifications on Android + existing backend, with **Copilot OFF** and **FCM OFF**.

Do **not** recommend full production rollout of Copilot or FCM.  
Do **not** claim staging PASS.  
Do **not** treat live production S3 as newly proven in AI.9.

---

## Status table

| Area | PASS | PARTIAL | BLOCKED | Evidence |
|------|------|---------|---------|----------|
| Staging Environment | | | X | No staging compose/deploy |
| S3 Upload | | | X | No safe non-prod credentials; not uploaded to prod |
| S3 Download | | X | | Local/AI.8 FS path PASS; live S3 not re-run |
| Result Security | X | | | AI.8 owner/other 403 retained |
| Email Without Attachment | X | | | Completion path + prior regression |
| Notification Deep Links | X | | | API `real_booking_id` + Android resolver + tests |
| Notification E2E | | X | | In-app path OK; FCM open/bg/closed N/A |
| FCM | | | X | No Firebase credentials |
| Copilot Read | | X | | Code/tests PASS; staging live BLOCKED |
| Copilot Booking | | | X | No staging enablement |
| Copilot Cancellation | | | X | No staging enablement |
| Copilot Authorization | X | | | AI.8 + pytest |
| Copilot Audit | X | | | AI.8 + pytest |
| Copilot Failure Handling | X | | | Tool deny/`ok:false` tests; no false success claim |
| Android Release Build | X | | | `assembleRelease` SUCCESS; prod API in BuildConfig |
| Backend Regression | X | | | 34 passed |
| Frontend Regression | X | | | No AI.9 frontend delta (`86cb60d`) |
| Staged Pilot | | | X | Staging absent |

---

## Git SHAs (post AI.9)

| Repo | Branch | SHA |
|------|--------|-----|
| Backend | `feature/ai-copilot-android` | `b0b44aa` |
| Android | `master` | `233740a` |
| Frontend | `feature/r6-remote-analysis-software-centric` | `86cb60d` (unchanged) |
