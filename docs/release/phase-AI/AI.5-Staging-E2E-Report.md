# AI.5 Staging / E2E / Production Readiness Report

**Date:** 2026-08-10  
**Mode:** AUTO MODE — evidence-based validation (no fabricated PASS)  
**Baseline:** AI.4 SHAs Backend `5d9752f` · Frontend `86cb60d` · Android `408a4d9`  

**AI.5 SHAs (this phase):**

| Repo | Branch | SHA |
|------|--------|-----|
| Backend | `feature/ai-copilot-android` | `7bb80fc` |
| Frontend | unchanged | `86cb60d` |
| Android | `master` | `06478e6` |

---

## 1. Environment

| Component | Evidence |
|-----------|----------|
| Backend HEAD | `feature/ai-copilot-android` @ `5d9752f` (clean) |
| Frontend HEAD | `feature/r6-remote-analysis-software-centric` @ `86cb60d` (local unrelated dirty files **not** touched) |
| Android HEAD (pre-AI.5) | `master` @ `408a4d9` |
| JDK | **FOUND** — `C:\Program Files\Java\jdk-17` (`java 17.0.12`) |
| Gradle wrapper | **WORKS** — Gradle 8.9 downloaded and `--version` OK |
| Android SDK / `ANDROID_HOME` / `adb` | **MISSING** — no SDK path, no emulator |
| Firebase / `google-services.json` | **MISSING** — only `google-services.json.example` |
| Controlled test accounts | **NOT AVAILABLE** — none documented/invented |
| Production portal | `https://equip.iitr.ac.in` reachable |
| Prod Copilot flag | **`research_copilot: false`** via `/api/v1/provisioning/capabilities/` |
| Prod Copilot version | `research_copilot_version: "0.0.0"` via `/api/version/` |

---

## 2. Android build

| Step | Result |
|------|--------|
| `java -version` | PASS (17.0.12) |
| `gradlew.bat --version` | PASS (8.9) |
| `gradlew.bat assembleDebug` | **FAIL** — `SDK location not found` (needs `local.properties` `sdk.dir` / `ANDROID_HOME`) |
| `gradlew.bat test` / `lint` / `installDebug` | **NOT RUN** (blocked by SDK) |
| Emulator | **BLOCKED** — no SDK/emulator |

**Verdict: Android Build = BLOCKED BY ENVIRONMENT (Android SDK)**  
JDK is no longer the blocker; **Android SDK is**.

Log excerpt (`ai5-assembleDebug.log`):

```text
FAILURE: Build failed with an exception.
Could not determine the dependencies of task ':app:compileDebugJavaWithJavac'.
> SDK location not found. Define a valid SDK location with an ANDROID_HOME
  environment variable or by setting the sdk.dir path in local.properties
```

---

## 3. Emulator

**BLOCKED BY ENVIRONMENT** — Android SDK / emulator / `adb` unavailable.  
No installDebug, no UI smoke, no offline toggle test on device.

---

## 4. Authentication (code + static)

| Check | Status | Evidence |
|-------|--------|----------|
| Login API client | Code present | `POST /auth/login/` |
| Token encrypted store | Code present | `EncryptedSharedPreferences` AES256_GCM |
| Password storage | Not stored | Only token + user JSON in secure prefs |
| Persist across restart | Code intent | Token restored on cold start |
| Logout | Code present | Clears store + calls `/auth/logout/` |
| 401 → login | Code present | `UnauthorizedHandler` clears token |
| Emulator E2E login/persist | **REQUIRES CONTROLLED E2E + SDK** | Not executed |

**Verdict: Android Login / Persistent Login = BLOCKED (E2E) / PARTIAL (code)**

---

## 5–11. Booking / Cancel / Operator / Sample / Completion / Results

All remain **code-complete from AI.4** but **REQUIRES CONTROLLED E2E** (test account + running app + preferably non-production staging).

No production bookings were created.

---

## 12–13. Notifications / Deep links

| Check | Status |
|-------|--------|
| In-app notification list parsing | Code (AI.4) |
| Deep-link heuristic to booking detail | Code (AI.4) |
| Live notification E2E | **REQUIRES CONTROLLED E2E** |
| FCM push delivery | **BLOCKED BY CREDENTIALS** |

---

## 14. Copilot

| Check | Status | Evidence |
|-------|--------|----------|
| Production enabled | **OFF** | capabilities `research_copilot:false` |
| Feature flag default | **false** | `config/settings/base.py` |
| Static auth scoping | **PASS (static)** | bookings/wallet/cancel scoped `user=user`; conversations `user=request.user` |
| Mutating tools force confirmation | **PASS (static)** | `_prepare_create_booking` / cancel / launch — no direct `objects.create` |
| Live Copilot queries / action cards | **REQUIRES staging + flag + OpenAI** | Not executed (kept disabled) |
| Pytest suite | **BLOCKED BY ENVIRONMENT** | See §17 |

---

## 15. FCM

**BLOCKED BY CREDENTIALS**

Required to proceed:

1. Firebase project for `ac.in.iitr.iicbooking`
2. Local (gitignored) `app/google-services.json`
3. Backend `FCM_SERVER_KEY` (or `FIREBASE_CREDENTIALS_JSON`)
4. Uncomment Google Services Gradle plugin
5. Device register → push smoke (fg/bg/killed) + invalid token cleanup

No push delivery claimed.

---

## 16. Security (static)

| Item | Result |
|------|--------|
| Copilot cannot list other users' bookings (code) | `Booking.objects.filter(user=user)` |
| Copilot wallet scoped | `Wallet.objects.filter(user=user)` |
| Conversation isolation | `get_object_or_404(..., user=request.user)` |
| Cross-user live probe | **REQUIRES CONTROLLED E2E** |
| Android secrets in repo | No `google-services.json`; no hardcoded FCM key |

---

## 17. Offline testing

**BLOCKED** — requires emulator/device.  
Code has `ConnectivityObserver` + offline banner; actions require Retrofit responses (no optimistic false success in repositories by design). Not device-proven.

---

## 18. Backend regression / tests

Attempts:

1. Host Python 3.14 + SQLite → **ERROR** (`near "DO": syntax error` — Postgres-specific SQL in migrations)
2. Host → Docker hostname `postgres` → **ERROR** (host cannot resolve compose DNS)
3. Docker network + image venv + fresh DB `iic_booking_test_ai5` → migrate setup **ERROR**:  
   `FieldError: Cannot resolve keyword 'timezone' into field` on `CrontabSchedule`  
   (legacy celery-beat schedule migrations vs installed `django-celery-beat` model)

Static (no migrate) checks: **PASS** — `STATIC_COPILOT_AUTH_PASS`, migration files present, flag default false.

**Verdict: Backend Regression = BLOCKED BY ENVIRONMENT** (test DB migrate / celery-beat timezone mismatch). Not a green pytest PASS.

---

## 19. Migration state

Files present on branch:

- `research_copilot/migrations/0001_initial_research_copilot.py`
- `research_copilot/migrations/0002_knowledge_engine.py`
- `communication/migrations/0053_pushdevice.py`
- `equipment/0186_auto_completion_data_detected_schedule.py` (timezone kw removed; other older schedule migrations still pass `timezone=`)

Production data was **not** modified. No `--fake` used.

---

## 20. API environment fix (AI.5 code change)

Previously debug builds silently inherited **production** `API_BASE_URL`.

Now:

| Build | Default API |
|-------|-------------|
| **release** | `https://equip.iitr.ac.in/api/` (`API_ENVIRONMENT=production`) |
| **debug** | `http://10.0.2.2:8000/api/` (`local`) unless overridden by `-PapiBaseUrl` or `local.properties` `api.base.url=` |

Profile screen shows API environment + base URL.  
**Does not change production release behaviour.**

---

## 21. Known limitations

1. Android SDK still missing → cannot assemble/test/install.
2. No controlled test user/operator credentials → no booking/sample E2E.
3. No Firebase credentials → FCM remains blocked.
4. Pytest migrate fails on celery-beat `CrontabSchedule.timezone` in this local Docker image combo.
5. Copilot must stay disabled in production until staging E2E + security probes pass.
6. Frontend working tree has unrelated dirty files — left untouched.

---

## 22. Production recommendation

**DO NOT enable** in production yet:

- `RESEARCH_COPILOT_ENABLED`
- FCM production delivery
- Android store/release against unvalidated debug flows

**Gate status:** **NOT READY** — production enablement criteria from Phase 20 are **not** met.

### Minimum remaining path

1. Install Android SDK 35 + create `local.properties` → `assembleDebug` / `test` / emulator  
2. Point debug at **staging/local** backend (not prod)  
3. Provide controlled user + operator accounts  
4. Execute AI.5 Phase 5–16 E2E checklist  
5. Fix/align celery-beat vs migrations for CI pytest green  
6. Optional: Firebase → FCM smoke  

---

## Final status table

| Area | PASS | PARTIAL | BLOCKED | Evidence |
|------|------|---------|---------|----------|
| Android Build | | | **X** | JDK OK; `assembleDebug` SDK location not found |
| Android Login | | **X** | | Code + secure store; E2E not run |
| Persistent Login | | | **X** | Requires emulator restart E2E |
| Booking | | | **X** | Requires controlled E2E |
| Cancellation | | | **X** | Requires controlled E2E |
| Operator Dashboard | | | **X** | Requires controlled E2E |
| Sample Acceptance | | | **X** | Requires controlled E2E |
| Sample Rejection | | | **X** | Requires controlled E2E |
| Booking Completion | | | **X** | Requires controlled E2E |
| Results | | | **X** | Requires controlled E2E |
| Notifications | | | **X** | Requires controlled E2E |
| Notification Deep Links | | **X** | | Code present; tap E2E not run |
| Copilot | | **X** | | Prod OFF; static auth PASS; live blocked |
| Copilot Tools | | **X** | | Static confirmation tools PASS; pytest blocked |
| Copilot Authorization | | **X** | | Static user-scoping PASS; live cross-user not probed |
| FCM | | | **X** | Credentials missing |
| Offline Handling | | | **X** | No emulator |
| Backend Regression | | | **X** | Pytest migrate FieldError timezone |

### Classification summary

| Bucket | Items |
|--------|-------|
| **COMPLETED (AI.5)** | Env inspection; JDK/Gradle proof; assembleDebug failure evidence; debug API env separation; Profile API env UI; static Copilot auth; prod flag confirmation; AI.5 report |
| **BLOCKED BY ENVIRONMENT** | Android SDK/emulator; backend pytest migrate (celery-beat timezone) |
| **BLOCKED BY CREDENTIALS** | FCM / Firebase |
| **REQUIRES CONTROLLED E2E** | Login persist, booking, cancel, operator samples, completion, results, notifications, offline, live Copilot |
| **DEFERRED** | Production Copilot/FCM enablement |
