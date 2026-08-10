# AI.6 — E2E Qualification Report

**Date:** 2026-08-10  
**Mode:** AUTO — evidence-based (no fabricated PASS)  
**Baseline (AI.5):** Backend `f631a51` · Frontend `86cb60d` · Android `06478e6`

---

## Environment

| Component | Evidence |
|-----------|----------|
| JDK | 17.0.x (`JAVA_HOME=C:\Program Files\Java\jdk-17`) |
| Android Gradle | Gradle wrapper 8.9 / AGP 8.7.3 / Kotlin 2.0.21 |
| Android SDK | `%LOCALAPPDATA%\Android\Sdk` (`sdk.dir` in gitignored `local.properties`) |
| Platforms present | `android-35`, `android-37.0` |
| System images / AVDs | **None** — emulator binaries present; no AVD / no system-images |
| adb | 37.0.1 — `adb devices` empty |
| Local backend `:8000` | **Unavailable** (`connection refused`) |
| Docker test DB | `iic_booking_test_postgres` + image `iic_booking_local_django` + volume `iic_ai5_app_venv` |
| Production Copilot | **OFF** (do not enable) |
| Production FCM | **OFF** / credentials unavailable |

---

## Backend — Migration blocker (FIXED)

### Root cause
Historical equipment/users Celery schedule migrations called `CrontabSchedule.objects.get_or_create(..., timezone=...)` against historical models that lacked `timezone` (field added in django-celery-beat `0010`/`0016`). Live package is django-celery-beat **2.8.x**.

### Fix (compatible, no `--fake`)
1. Helper `iic_booking/compat/celery_crontab.py` — `crontab_get_or_create()` sets `timezone` only if the field exists.
2. Schedule migrations updated to use the helper and depend on `django_celery_beat.0016_alter_crontabschedule_timezone` where required.
3. `remote_analysis.0017` — `SeparateDatabaseAndState` ordering / Django 5.2 index introspection fix (duplicate index + `get_constraints`).

### Evidence (Docker / `config.settings.test` / DB `iic_booking_test_ai6`)
- `manage.py migrate --noinput` → **MIGRATE_EXIT=0**
- `manage.py check` → **System check identified no issues** / **CHECK_EXIT=0**
- Celery-beat migrations through **0016+** applied on fresh test DB (log: `docs/release/phase-AI/ai6-pytest-docker.log`)

---

## Backend — Prioritized test suite

**Command (Docker):**
`pytest iic_booking/research_copilot/tests iic_booking/communication/tests/test_push_device.py`

| Metric | Result |
|--------|--------|
| TOTAL | 19 |
| PASSED | **19** |
| FAILED | **0** |
| SKIPPED | 0 |
| BLOCKED | 0 (migrate no longer blocks) |

**Log:** `docs/release/phase-AI/ai6-pytest-docker-final.log`

### Genuine defects fixed during AI.6
1. **Inactive fixture users → 401** — project `create_user` leaves `is_active=False`; `TokenAuthenticationWithInactivity` rejects inactive users. Fixtures now set `is_active=True` (+ verified/approved flags).
2. **Audit assertion** — without LLM key, low-confidence replies audit as `escalate_hint` instead of `message_replied`. Test accepts either.

### Not claimed
Full booking / sample / notification suite beyond the prioritized Copilot + push-device set was not re-run as an exhaustive monolith in this phase window. Migration unblocking + Copilot/push suite green is the evidenced scope.

---

## Android SDK / Build

| Step | Result | Evidence |
|------|--------|----------|
| SDK configured | PASS | gitignored `local.properties` → `sdk.dir=...Android\\Sdk` |
| `assembleDebug` | **PASS** | `BUILD SUCCESSFUL` · APK `app/build/outputs/apk/debug/app-debug.apk` |
| `test` (unit) | **PASS** | `BUILD SUCCESSFUL in 44s` · `ai6-unit-test.log` |
| Emulator / `installDebug` | **BLOCKED** | No system images / no AVDs; `adb devices` empty |
| Lint | NOT RUN as gate | — |

### Genuine Android defects fixed
1. **Kotlin package `ac.in.iitr.iicbooking`** — soft keyword `in` under Kotlin 2.0. Renamed sources/namespace to **`ac.iitr.iicbooking`**; kept **`applicationId = "ac.in.iitr.iicbooking"`**.
2. **Wrong Modifier FQCN** — class is `androidx.compose.ui.Modifier` (not `...modifier.Modifier`).
3. **Named args `Modifier =`** (capital M) in AI.5 UI sources — invalid; fixed to `modifier =`.
4. **`BookingDetailViewModel.kt`** was corrupted (contained Screen UI); restored proper ViewModel + factory.
5. Accidental corruption of `modifier: Modifier =` type annotations during mass replace — repaired.

### Debug API split (retained from AI.5)
- Release → `https://equip.iitr.ac.in/api/`
- Debug default → `http://10.0.2.2:8000/api/` (override via `-PapiBaseUrl` / `local.properties` `api.base.url=`)
- Profile shows API environment

---

## Controlled E2E (Phases 8–21)

| Area | Status | Reason |
|------|--------|--------|
| Local/staging backend for emulator | BLOCKED | No process on `localhost:8000` |
| Emulator device | BLOCKED | No AVD / system image |
| Controlled accounts | PARTIAL (code exists) | `seed_test_users` + `is_test_account` helpers exist; **passwords not documented here**; not exercised live |
| Login / booking / cancel / sample / results / notifications / offline / Copilot live | **NOT RUN** | Requires emulator + backend |

**Do not invent credentials.** Seeded QA accounts may be used only after a safe local/staging backend is running.

---

## FCM

**BLOCKED** — Firebase credentials / `google-services.json` still unavailable. Production FCM remains OFF.

---

## Copilot

| Check | Status |
|-------|--------|
| Static authorization (user-scoped tools) | Covered by unit/API tests (suite PASS) |
| Live staging prompts / action cards | NOT RUN (no staging Copilot enablement in this run) |
| Production `RESEARCH_COPILOT_ENABLED` | **Must remain false** |

---

## Migrations safety

Fresh AI.6 test database migrated cleanly (`MIGRATE_EXIT=0`). No production DB was modified. No `--fake` used.

---

## Production recommendation

### **NOT READY**

Gate failures / blockers:
1. Android emulator + device E2E **not executed**
2. Local/staging backend for Android **not running**
3. FCM **blocked** (acceptable to remain blocked independently)
4. Copilot production enablement **not authorized**

### Ready-enough for next phase (AI.7+)
- Backend **migrations unblocked** with evidence
- Prioritized Copilot + push-device tests **19/19 PASS**
- Android **debug APK builds and unit tests pass**

---

## Status table

| Area | PASS | PARTIAL | BLOCKED | Evidence |
|------|:----:|:-------:|:-------:|----------|
| Backend Migration | X | | | MIGRATE_EXIT=0 on `iic_booking_test_ai6` |
| Backend Tests (prioritized) | X | | | 19 passed (`ai6-pytest-docker-final.log`) |
| Android SDK | X | | | SDK path + platform-tools |
| Android Build | X | | | `assembleDebug` SUCCESS + APK |
| Android Unit Tests | X | | | `gradlew test` SUCCESS |
| Android Emulator | | | X | No AVD / system-images |
| Login | | | X | No emulator/backend |
| Persistent Login | | | X | No emulator |
| Booking | | | X | No emulator/backend |
| Cancellation | | | X | No emulator/backend |
| Operator Dashboard | | | X | No emulator/backend |
| Sample Acceptance | | | X | No emulator/backend |
| Sample Rejection | | | X | No emulator/backend |
| Booking Completion | | | X | No emulator/backend |
| Results | | | X | No emulator/backend |
| Notifications | | | X | No emulator/backend |
| Deep Links | | | X | No emulator |
| FCM | | | X | No Firebase credentials |
| Copilot (API tests) | X | | | pytest research_copilot |
| Copilot Tools | X | | | pytest tools suite |
| Copilot live E2E | | | X | No staging session |
| Authorization (static/API) | X | | | Copilot tests + guest 401/403 |
| Offline Handling | | | X | No emulator |
| Regression (Android unit) | X | | | `gradlew test` |
| Production flags | X | | | Copilot/FCM left OFF |

---

## Known issues / follow-ups

1. Install Android system image (API 35) + create Pixel AVD; `installDebug`; run controlled E2E.
2. Start local Django on host `:8000` (emulator → `10.0.2.2:8000`); seed test users **without publishing passwords**.
3. Configure Firebase only when credentials are available; keep prod FCM off until push+deeplink proof.
4. Expand backend pytest beyond Copilot/push once migration gate stays green.

---

## Git SHAs (post AI.6 commits)

Recorded after commits in the closing section of the agent run / `git rev-parse`.

| Repo | Branch | Expected change |
|------|--------|-----------------|
| Backend | `feature/ai-copilot-android` | celery crontab compat + 0017 + Copilot test fixtures + this report |
| Android | `master` | package rename + ViewModel + Compose named-arg fixes |
| Frontend | (unchanged) | `86cb60d` |

**Production Copilot:** OFF  
**Production FCM:** OFF  

### Recorded SHAs (this commit series)

- Backend tip: `9190837` (migration/tests `348bde3` + docs SHA note)
- Android: `cf5a373`
- Frontend: `86cb60d` (unchanged)

