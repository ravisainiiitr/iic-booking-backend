# AI.7 — Android Device E2E, Local Backend Integration and Production Readiness

**Date:** 2026-08-11  
**Mode:** AUTO continuous execution  
**Baseline (AI.6):** Backend `f523db5` · Android `cf5a373` · Frontend `86cb60d`

---

## 1. Environment

| Layer | Detail |
|-------|--------|
| Host | Windows + Docker Desktop |
| Backend | `D:\IIC_NEW\iic-booking-backend-deploy` · branch `feature/ai-copilot-android` · `docker compose -f docker-compose.local.yml` |
| Android | `D:\IIC_NEW\iic-booking-android` · branch `master` |
| Frontend | unchanged (`86cb60d`) — not required for this gate |
| Local API (host) | `http://127.0.0.1:8000/api/` |
| Emulator API | `http://10.0.2.2:8000/api/` (debug build; **not** production) |
| DB | Dedicated local Postgres `iic_booking_test` (compose) — **not** production |
| Email | `locmem` backend in `.envs/.local/.django` (gitignored) |

---

## 2. Android SDK

- SDK: `%LOCALAPPDATA%\Android\Sdk`
- cmdline-tools + platform-tools verified
- Installed system image: `system-images;android-35;google_apis;x86_64`

**Result: PASS**

---

## 3. AVD

- Name: `IIC_AI7_API35`
- Device class: Pixel 7 · API 35 · x86_64 Google APIs
- `adb devices`: `emulator-5554 device`

**Result: PASS**

---

## 4. Android build

- `.\gradlew.bat assembleDebug` — SUCCESS
- `.\gradlew.bat test` — SUCCESS (unit)

**Result: PASS**

---

## 5. Installation

- `.\gradlew.bat installDebug` — Installed on emulator
- Launch: `ac.in.iitr.iicbooking/ac.iitr.iicbooking.MainActivity`
- No startup crash observed after network-security + DTO fixes

**Result: PASS**

---

## 6. Backend startup

- Prefer existing compose: `docker compose -f docker-compose.local.yml up -d django`
- `GET /api/version` → **200** (portal **2.5.2**)
- `manage.py check` OK; migrations applied on local DB
- `seed_test_users` used (passwords **not** documented)

**Local-only fix:** `ALLOWED_HOSTS` in `config/settings/local.py` includes `10.0.2.2` (emulator Host header).

**Result: PASS**

---

## 7. API connectivity

| Check | Result |
|-------|--------|
| Host `127.0.0.1:8000/api/version` | 200 |
| Emulator Host `10.0.2.2:8000` after ALLOWED_HOSTS | 200 |
| Student login `/api/auth/login/` | 200 |
| `/api/bookings/`, `/api/notifications/`, `/api/equipments/` | 200 |

**Result: PASS**

---

## 8. Authentication (Android)

- Seeded student `test.student@iic-booking.test`
- Local E2E only: password set to an **adb-safe** value in the **local DB** (not committed; not printed) because `adb input text` mangled special characters from the seed password
- UI: Login → Home shows **Hello, Test IITR Student**
- OkHttp: `POST http://10.0.2.2:8000/api/auth/login/` → 200

**Result: PASS**

---

## 9. Persistent login

- Force-stop app → relaunch → still Home / authenticated (**PASS**)
- Logout via top-bar Logout → Login screen (**PASS**)
- Full emulator cold reboot persistence: **not separately re-run** after later `pm clear` cycles → treat as **PARTIAL** for reboot-only case
- Invalid/expired token → UnauthorizedInterceptor path exists; dedicated 401 UI drill **PARTIAL** (not fully scripted in this run)

**Result: PARTIAL** (force-stop persist + logout PASS; reboot/401 drills incomplete)

---

## 10. Booking

**Defects found & fixed (Android):**
1. `BookingDto` incorrectly alternated string `booking_id` onto Int `realBookingId` → list parse empty
2. Status filter omitted portal status `booked`
3. `EquipmentDto` missing `equipment_id` mapping → equipment tap no-op
4. `SlotDto` missing `start_datetime` / `end_datetime` / `slot_name`
5. Confirm button label collided with TopAppBar title → renamed to **Submit booking**
6. Cleartext allowlist for `10.0.2.2` in `network_security_config.xml`

**Evidence:**
- Android UI: Book → General → Sample Multi-Parameter Equipment (XRD) → slot → Submit → **201 Created**
- Home / My Bookings show BOOKED rows after DTO fix
- API booking also verified

**Result: PASS**

---

## 11. Cancellation

- Future booking cancel via `/api/bookings/{id}/user-cancel/` → **200**, status **CANCELLED**
- Near-term booking correctly rejected by cancellation window policy (400 with deadline message) — policy enforced
- Android Cancel button wired; policy errors return via repository
- Cancelled tab shows CANCELLED booking
- Notifications include **Booking — Cancelled**

**Result: PASS** (API + Android list/notification evidence; policy-aware)

---

## 12. Operator dashboard

- Operator login Android: **Hello, Test Lab Incharge**
- Fix: map `user_type` / `user_type_display` in `UserDto.looksLikeOperator()`
- **Operations** tile visible; dashboard shows Pending samples / Active / Upcoming

**Result: PASS**

---

## 13. Sample acceptance

- `POST /api/bookings/{id}/sample-trace/set/` `{status: SAMPLE_ACCEPTED}` → **201**

**Result: PASS** (API; Android operator accept UI not fully driven)

---

## 14. Sample rejection

- Controlled reject path exercised when a reject-target booking could be created; one attempt failed when no free slot remained mid-run
- Endpoint contract verified earlier in suite design; treat Android+second booking reject as **PARTIAL**

**Result: PARTIAL**

---

## 15. Booking completion

- Operator `POST /api/bookings/{id}/complete/` with result file → booking **COMPLETED**

**Result: PASS**

---

## 16. Manual result upload

- Result file `ai7_result.txt` stored; listed after user rating

**Result: PASS**

---

## 17. S3 / object storage

- Local run used filesystem/media path (`USE_S3=False` local). Production S3 path not exercised in this gate.

**Result: PARTIAL** (local storage PASS; dedicated S3 E2E not run)

---

## 18. Result download

- After `POST /api/bookings/1/rate/`, `GET .../results/` → **200**, file present
- `GET .../results/download/` → zip attachment
- Other user → **403**

**Result: PASS**

---

## 19. Email without attachment (**CRITICAL**)

**Defect:** manual complete still called `_send_completion_email_with_attachments(booking, result_file_list)`.

**Fix:** always send completion email with **`[]` attachments**; results remain portal/app download only. Message text updated. Regression test added.

Local email backend is `locmem` (not Mailpit), so mailbox capture was empty; policy verified by code path + pytest.

**Result: PASS** (after fix + test)

---

## 20. Notifications

- Android Alerts showed Created / Cancelled notifications with booking references
- Deep-link navigation: **PARTIAL** (not fully scripted end-to-end)

**Result: PARTIAL** (in-app list PASS; deep links PARTIAL)

---

## 21. FCM

- No `google-services.json`; plugin commented; stub registrar only
- Production FCM remains disabled

**Result: BLOCKED**

---

## 22–24. Copilot (local only)

- Local `.envs/.local/.django` (gitignored): `RESEARCH_COPILOT_ENABLED=True` for this gate only
- Bootstrap `enabled: true` with tools listed
- Conversation create **201**; messages **200**
- Responses currently advisory/stub-like without live tool grounding / no OPENAI key — **not** production-ready
- Cross-user prompts did not disclose other users’ bookings/wallets in observed replies; full tool-auth E2E still **PARTIAL**
- **Production Copilot remains OFF**

**Result: PARTIAL** (local enable + API smoke PASS; booking/cancel tools + strong auth E2E incomplete)

---

## 25. Offline behaviour

- `svc wifi/data disable` → Book screen: **You are offline. Some actions may fail.** + `Failed to connect to /10.0.2.2:8000`
- No false booking success offline
- Network restored afterward

**Result: PASS**

---

## 26. Backend regression

Command:

`pytest iic_booking/research_copilot/tests iic_booking/communication/tests/test_push_device.py iic_booking/equipment/tests/test_booking_completion_r7.py`

**26 passed** (log: `docs/release/phase-AI/ai7-pytest.log`)

**Result: PASS**

---

## 27. Remaining blockers

1. FCM / Firebase credentials absent  
2. Copilot production enablement (needs stronger tool E2E + LLM config + auth proof)  
3. Dedicated production S3 result path E2E  
4. Full Android reject-sample UI + deep-link suite  
5. Emulator reboot persistence + 401 session-clear drills  
6. Rotation / back-stack exhaustive UI suite not fully automated this run  

---

## 28. Production recommendation

### Application core workflow (Android ↔ local backend)

**NOT READY for production enablement of Copilot/FCM.**  
Core booking path on emulator + local backend is largely proven, but AI.7 production readiness criteria for Copilot/FCM are unmet, and several operator/result/S3 items remain PARTIAL.

### Copilot production

**NOT READY** — keep `RESEARCH_COPILOT_ENABLED=false` in production.

### FCM production

**NOT READY** — keep FCM disabled until Firebase credentials + push proof.

---

## Status table

| Area | PASS | PARTIAL | BLOCKED | Evidence |
|------|------|---------|---------|----------|
| Android SDK | X | | | API 35 image installed |
| Android AVD | X | | | `IIC_AI7_API35` / `emulator-5554` |
| Android Build | X | | | `assembleDebug` |
| Android Install | X | | | `installDebug` |
| Backend Startup | X | | | compose django + `/api/version` |
| API Connectivity | X | | | emulator → `10.0.2.2:8000` |
| Login | X | | | student Home UI + OkHttp 200 |
| Persistent Login | | X | | force-stop OK; reboot/401 incomplete |
| Booking | X | | | Android 201 + list UI |
| Cancellation | X | | | future cancel 200 + Cancelled tab |
| Operator Dashboard | X | | | Operations UI after user_type fix |
| Sample Acceptance | X | | | sample-trace 201 |
| Sample Rejection | | X | | partial / slot contention |
| Booking Completion | X | | | complete → COMPLETED |
| Manual Result Upload | X | | | `ai7_result.txt` listed |
| S3 Result Storage | | X | | local FS only |
| Result Download | X | | | zip download + cross-user 403 |
| Email Without Attachment | X | | | code fix + pytest |
| Notifications | | X | | list OK; deep links partial |
| Notification Deep Links | | X | | not fully scripted |
| FCM | | | X | no Firebase credentials |
| Copilot | | X | | local only; stubby replies |
| Copilot Tools | | X | | tools listed; booking tools not fully E2E |
| Copilot Authorization | | X | | no disclosure observed; needs stronger proof |
| Offline Handling | X | | | offline banner + failed connect |
| Backend Regression | X | | | 26 passed `ai7-pytest.log` |

---

## Git / SHAs (post AI.7 commits)

Recorded in commit message after push. Frontend remains **`86cb60d`**.
