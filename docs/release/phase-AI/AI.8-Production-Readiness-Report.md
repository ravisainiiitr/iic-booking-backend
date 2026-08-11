# AI.8 — Close AI.7 Gaps + Staging Qualification + Copilot/Result/Notification Hardening

**Date:** 2026-08-11  
**Mode:** AUTO continuous execution  

## 1. AI.7 baseline

| Repo | Branch | SHA |
|------|--------|-----|
| Backend | `feature/ai-copilot-android` | `efe1897` |
| Android | `master` | `f5e5e7b` |
| Frontend | `feature/r6-remote-analysis-software-centric` | `86cb60d` |

AI.7 report: `docs/release/phase-AI/AI.7-Android-E2E-Qualification-Report.md`

Verified AI.7 PASS retained: emulator ↔ local Django booking lifecycle, completion email **without** result attachment, prioritized pytest green.

---

## 2. Persistent login

| Scenario | Result | Evidence |
|----------|--------|----------|
| A Login → force-stop → reopen | **PASS** | Home `Hello, Test IITR Student` |
| B Login → emulator reboot → reopen | **PASS** | `persist_reboot=True` after `adb reboot` |
| D Logout → reopen | **PASS** (AI.7 + 401 path) | Login required |

Token store: `TokenStore` / `EncryptedSharedPreferences` (`iic_booking_secure_prefs`).

**Result: PASS**

---

## 3. 401 handling

Server-side deleted DRF `Token` for student while app session active → opened Bookings → app cleared secure session → **Sign in** screen. No infinite retry loop observed.

`UnauthorizedInterceptor` clears `TokenStore` and notifies `IicBookingAppRoot`.

**Result: PASS**

---

## 4. Sample rejection UI

Android Operations screen updated:

- Labels: **Accept Sample** vs **Reject Sample**
- Rejection reason required before confirm
- **AlertDialog** confirmation: *Confirm Reject Sample* / *Keep Sample* (prevents accidental reject)

Operator dashboard opened with reason field visible.

Live pending-queue reject tap was empty this run (no pending samples in local queue after prior rejects). API path fully proven (below).

**Result: PASS** (UI hardening + confirmation); live pending-card reject = covered by API when queue empty.

---

## 5. Sample rejection API

`POST /api/bookings/{id}/sample-trace/set/`

- No reason → **400** `A reason is required for this status.`
- `{status: SAMPLE_REJECTED, reason: ...}` → **201** (refund + notification)

**Result: PASS**

---

## 6. S3 production path

Code path exists: `iic_booking/sync/services/results_s3.py` (`upload_local_file_to_results_s3`, `presign_results_s3_get`, keyed by virtual booking id).

Local compose: `AWS_STORAGE_BUCKET_NAME` empty → **FileSystemStorage** used for manual result upload. Safe local upload/download proven.

**No staging compose** and **no safe staging AWS credentials** in this environment → live production-bucket E2E **not executed** (would risk secrets/production data).

**Result: PARTIAL** (implementation + local storage PASS; live S3 bucket E2E blocked by environment)

---

## 7. Result security

| Actor | Endpoint | Status |
|-------|----------|--------|
| Owner | `GET /api/bookings/{id}/results/` | 200 |
| Owner | `GET .../results/download/` | 200 (zip) |
| Other seeded user | results + download | **403** |

Authorization enforced server-side.

**Result: PASS**

---

## 8. Result download

Owner download after rating succeeded (`ai8_result.txt` listed). Cross-user denied.

**Result: PASS**

---

## 9. Email without attachment (regression)

Manual complete response message:

`... result file(s) uploaded for portal/app download (not emailed as attachments).`

Code path continues to call `_send_completion_email_with_attachments(booking, [])`. Pytest regression retained.

**Result: PASS**

---

## 10. Notification deep links

In-app Alerts list shows Created / Refunded / Cancelled.

Tap navigates to **My Bookings** (portal links use virtual booking codes `?booking=GENERALSAMPLE-...`, not numeric PKs). Deep-link parser improved to handle numeric IDs + virtual-code fallback to My Bookings.

Foreground in-app path exercised. FCM not available.

**Result: PARTIAL** (in-app navigation works; virtual-code → exact Booking Detail still fallback)

---

## 11–16. Copilot

Local only: `RESEARCH_COPILOT_ENABLED=True` in gitignored `.envs/.local/.django`. **Production remains false.**

### Fixes in AI.8
- `search_bookings` / `cancel_booking` / `launch_remote_analysis` fixed for Booking PK (`booking_id`) and slot schedule
- Reject foreign selectors (`email` / `user_id`) on bookings + wallet → `forbidden`
- Role check + **tool audit** (`TOOL_EXECUTED` / `TOOL_DENIED`)
- `individual_student` mapped to student role bucket

### Results
| Area | Result | Notes |
|------|--------|-------|
| Copilot Read (`search_bookings`) | **PASS** | scoped to caller; n≥1 |
| Copilot Booking tool | **PASS*** | returns confirmation card + href; **does not bypass** booking API (by design) |
| Copilot Cancellation tool | **PASS*** | own booking → confirmation; other user → `booking_not_found` |
| Copilot Authorization | **PASS** | foreign email/user_id denied; cancel other denied |
| Copilot Audit | **PASS** | `CopilotAuditEvent` written on tool execute |
| Copilot Failure Handling | **PASS** | unknown tool / forbidden / not found return `ok:false` |

\*Mutating tools prepare portal action cards only — actual mutation remains existing booking/cancel APIs after user confirmation.

**Production Copilot: NOT READY** (no staging LLM/key proof; tools are confirmation-oriented; keep flag off).

---

## 17. FCM

`google-services.json` still only `.example`; plugin commented; stub registrar.

**Result: BLOCKED**

---

## 18. Backend regression

```
pytest iic_booking/research_copilot/tests \
       iic_booking/communication/tests/test_push_device.py \
       iic_booking/equipment/tests/test_booking_completion_r7.py
```

**28 passed** (log: `docs/release/phase-AI/ai8-pytest.log`, gitignored if matching ignore rules)

`manage.py check` OK.

**Result: PASS**

---

## 19. Android regression

`.\gradlew.bat test assembleDebug installDebug` — SUCCESS  
Emulator exercises: Home, Bookings, Alerts, Operations, Copilot nav present, offline banner path retained from AI.7.

**Result: PASS**

---

## 20. Staging

No `docker-compose.staging.yml`. Deploy path is production EC2 workflow / tag-based. AI.8 did **not** deploy to production.

**Result: BLOCKED / N/A** for staging deploy in this environment (assessment only).

---

## 21. Remaining blockers

1. Live AWS S3 bucket E2E (credentials/staging env)
2. FCM / Firebase credentials
3. Virtual booking-code → numeric Booking Detail deep link resolution
4. Production Copilot enablement (LLM + fuller action execution policy decision)
5. Dedicated staging environment deploy

---

## 22. Production recommendation

### Final decision: **READY FOR STAGED PILOT**

| Track | Decision |
|-------|----------|
| **CORE PLATFORM** (Android ↔ backend booking / sample / complete / results / auth / offline) | **READY FOR STAGED PILOT** |
| **COPILOT** | **NOT READY** for production — keep `RESEARCH_COPILOT_ENABLED=false` |
| **FCM** | **BLOCKED** — keep disabled |

Do **not** enable production Copilot or FCM based on AI.8.

---

## Status table

| Area | PASS | PARTIAL | BLOCKED | Evidence |
|------|------|---------|---------|----------|
| Persistent Login | X | | | force-stop + emulator reboot |
| 401 Handling | X | | | token delete → Sign in |
| Sample Rejection UI | X | | | Accept/Reject labels + confirm dialog |
| Sample Rejection API | X | | | 400 without reason; 201 with reason |
| S3 Production Path | | X | | code ready; local FS only |
| Result Security | X | | | owner 200 / other 403 |
| Result Download | X | | | zip download after rating |
| Email Without Attachment | X | | | complete message + `[]` attachments |
| Notification Deep Links | | X | | My Bookings fallback for virtual codes |
| Copilot Read | X | | | `search_bookings` scoped |
| Copilot Booking | X | | | confirmation card / existing API |
| Copilot Cancellation | X | | | own confirm / other denied |
| Copilot Authorization | X | | | forbidden on foreign selectors |
| Copilot Audit | X | | | TOOL_EXECUTED events |
| Copilot Failure Handling | X | | | ok:false codes |
| FCM | | | X | no Firebase credentials |
| Backend Regression | X | | | 28 passed |
| Android Regression | X | | | test + assembleDebug + install |
| Staging | | | X | no staging compose/deploy in env |

---

## Git / SHAs (post AI.8)

Recorded after commit/push below.
