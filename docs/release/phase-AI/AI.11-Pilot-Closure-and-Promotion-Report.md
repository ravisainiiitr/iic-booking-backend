# AI.11 — Pilot Closure and Promotion Report

**Date:** 2026-08-11  
**Mode:** AUTO continuous execution  
**Decision:** **READY FOR LIMITED PRODUCTION PILOT — EXTENDED**

Not promoted to broader production.

---

## 1. AI.10 baseline

| Repo | Branch | SHA |
|------|--------|-----|
| Backend | `feature/ai-copilot-android` | `bed0c68` |
| Android | `master` | `233740a` |
| Frontend | `feature/r6-remote-analysis-software-centric` | `86cb60d` |

AI.10 docs: runbook, support matrix, checklist, readiness report under `docs/release/phase-AI/`.

Inherited: CORE ready for limited pilot; Copilot OFF; FCM blocked; S3 live E2E blocked; staging blocked.

AI.11 did **not** enable Copilot/FCM, invent staging, invent test users, modify production booking data, or redesign DSA/RAA.

---

## 2. Migration verification

**Mechanism:** new read-only Actions workflow `Show Production Migrations` (merged to `master`, dispatched on EC2 self-hosted runner).  
**Does not** run `migrate`.

| App | Production result |
|-----|-------------------|
| `communication` | All listed migrations **[X]** applied |
| `remote_analysis` (incl. R9 `0022_r9_workstation_data_workspace`, `0023_…`) | All listed **[X]** |
| `device_provisioning` | All listed **[X]** |
| `sync` | All listed **[X]** |
| `equipment` | All listed **[X]** |
| `research_copilot` | **Not installed** on production (`No installed app with label 'research_copilot'`) — expected while Copilot OFF |

Pending grep: **No pending migrations listed for selected apps.**

Run evidence: GitHub Actions run `31451515654` (success).

Runtime flags from same job:

- `RESEARCH_COPILOT_ENABLED=False`
- `FCM_SERVER_KEY_configured=False`
- `INSTALLED_APPS_has_research_copilot=False`
- Root disk ~**53%** used (49G)

**Migrations: PASS** (core apps). Copilot schema intentionally absent on prod.

---

## 3. Backup verification

**Mechanism:** `AI11 Production Observability Sample` workflow (run `31451306949`, success).

| Item | Status |
|------|--------|
| Backup mechanism | Nightly cron script on EC2 |
| Frequency | Nightly (~02:30) |
| Latest successful dump | `nightly-20260810` (`PASS dump size=16M`) |
| Destination | `/home/ubuntu/backups/nightly/…`; `latest` → `nightly-20260810` |
| Log | `/var/log/iic-nightly-backup.log` PRESENT |
| Scripts | `iic-nightly-backup-cron.sh`, `iic-restore-verify.sh` PRESENT |
| Retention | Documented 14 days (phase-M) |
| Encryption | NOT VERIFIED from sample |
| Restore verification | **NOT VERIFIED** (no destructive / temp-restore executed in AI.11) |

Log shows successive PASS dumps 2026-08-06 … 2026-08-10 (sizes 8.1M → 16M).

**Backups: PARTIAL** — latest dump success PASS; restore test NOT VERIFIED.

---

## 4. DSA live smoke

No enrolled Equipment PC / DSA host was available to this agent session for controlled live smoke (online/offline/heartbeat/IP identity).

Did **not** invent registrations or alter production device rows.

**DSA: BLOCKED** (live smoke) — requires lab-controlled enrolled DSA + Equipment PC.

API/production health remains ready (see §12/§13).

---

## 5. Equipment PC live smoke

Blocked for the same reason: no safe controlled booking account + physical Equipment PC path exercised in AI.11.

Did **not** manually mkdir booking folders or invent bookings.

**Equipment PC: BLOCKED** (live smoke)

---

## 6. Workspace verification

Blocked pending Equipment PC / DSA live smoke.

**Workspace: BLOCKED / NOT VERIFIED**

---

## 7. Results

Retained AI.7–AI.10 evidence:

- Completion email attachments empty (code + tests)
- Authorized download / cross-user 403
- Temp cleanup after successful S3 publish

No new production result upload performed.

**Results (code/regression): PASS**  
**Results (live equipment path): NOT VERIFIED**

---

## 8. S3

Safe non-production S3 credentials still **not available** in this environment.

**S3 LIVE E2E: BLOCKED**

---

## 9. Android signing

| Item | Status |
|------|--------|
| `applicationId` | `ac.in.iitr.iicbooking` |
| `versionCode` / `versionName` | `1` / `1.0.0` |
| Release API | `https://equip.iitr.ac.in/api/` |
| `signingConfigs` / keystore | **ABSENT** |
| `keystore.properties` | ABSENT |
| Artifact | `app-release-unsigned.apk` only |
| `google-services.json` | ABSENT |

`.\gradlew.bat test assembleRelease` — **SUCCESS** (unsigned).

**Android Release (unsigned config): PASS**  
**Android Signed APK: BLOCKED** (no keystore; do not invent one)

---

## 10. Android device test

AI.10 emulator smoke retained (debug→local): Home / Bookings / Alerts / Profile / force-stop persist.

Signed prod install: **NOT RUN** (no signed APK).

**Android device (signed): BLOCKED**

---

## 11. Notifications

Deep links + `real_booking_id` remain PASS (AI.9).  
Email + in-app remain production path while FCM blocked.  
Production Copilot app absent — no Copilot notification dependency.

**Notifications: PASS** (in-app/email + deep links)

---

## 12. Remote Analysis

Production `/api/v1/analysis/health/ready/` → **200** `ready`  
Checks include database/cache ok; guacamole ok; gateway ok; reverse_tunnel configured.

R9 Data Workspace **live** qualification still **NOT QUALIFIED** as an end-to-end lab exercise (migrations applied does not equal live workspace PASS).

**Remote Analysis: PARTIAL**

---

## 13. Security

| Check | Result |
|-------|--------|
| Copilot disabled in prod settings | PASS |
| FCM key not configured | PASS |
| Frontend Copilot gated by `VITE_RESEARCH_COPILOT_ENABLED==="true"` | PASS (must not be true in prod build) |
| Security pytest (`test_security_hardening` + AI suite) | Included in **43 passed** |
| Log sample secret dump | Redacted sampler; no password/token/AWS key patterns called out in AI.11 sample |
| Unauthorized result download | Prior AI.8 PASS retained |

**Security: PASS** (sanity + tests); continuous log redaction audit remains ops practice.

---

## 14. Monitoring

Observability sample (django logs tail 200):

- Dominant signal: `monitoring.health_collection` snapshots with `alerts=0`
- One transient `alerts=1` then recovery — classified **Known/expected brief operational blip**
- **0** matches for HTTP 5xx / ERROR / Traceback in the sampled window

Host disk 53% — no exhaustion signal.

Full Celery/Redis deep dive: not exhaustively scraped; no smoking gun in sample.

**Monitoring: PARTIAL** (sample PASS; not a full SIEM review)

---

## 15. Pilot metrics

No new analytics subsystem. Existing signals available to ops:

| Metric source | AI.11 note |
|---------------|------------|
| Nightly backup sizes | Growing 8→16M (activity proxy) |
| Monitoring snapshots | Continuous agent health collection |
| Booking counts / rejects / downloads | Require DB/admin queries on host — **NOT VERIFIED** here (no prod data scrape) |

**Pilot metrics: NOT VERIFIED** (aggregates)

---

## 16. Remaining blockers (broader production)

1. **DSA live smoke** on enrolled lab path  
2. **Equipment PC live smoke** + workspace on real PC (LAN-only)  
3. **Signed Android APK** + controlled device install  
4. **S3 live E2E** with safe controlled credentials  
5. **Backup restore verification** (`iic-restore-verify.sh` / weekly VERIFY_RESTORE_DB)  
6. **Copilot** remains NOT READY (intentionally)  
7. **FCM** remains BLOCKED BY CREDENTIALS  

---

## 17. Promotion gates

| Gate | Requirement | AI.11 result |
|------|-------------|--------------|
| **A CORE** | Health, regression, FE build, Android release, booking/sample/results/notifications, **DSA smoke**, **Equipment PC smoke** | Core automated **PASS**; **DSA/Equipment smoke FAIL gate** |
| **B DATA** | S3 live E2E PASS or accepted deferral | **BLOCKED** — may defer for limited pilot only |
| **C OPS** | Backup verified or accepted risk | Dump success **PASS**; restore **NOT VERIFIED** — accept risk only for limited extension |
| **D MOBILE** | Signed APK PASS or accepted pending | **BLOCKED** — unsigned only |
| **E AI** | Copilot OFF | **PASS** |
| **F PUSH** | FCM may remain blocked | **PASS** (non-blocking) |

### Final decision: **READY FOR LIMITED PRODUCTION PILOT — EXTENDED**

| Layer | Status |
|-------|--------|
| CORE PLATFORM | READY for continued **limited** pilot |
| OPERATIONAL BLOCKERS | DSA/Equipment live smoke, signed APK, S3 E2E, backup restore verify |
| COPILOT | NOT READY — keep OFF |
| FCM | BLOCKED |
| BROADER PRODUCTION | **NOT READY** until Gate A edge smokes + Gate D signing (and preferably B/C) close |

---

## Status table

| Area | PASS | PARTIAL | BLOCKED | NOT VERIFIED | Evidence |
|------|------|---------|---------|--------------|----------|
| Production Health | X | | | | version/ready/home 200 |
| Migrations | X | | | | Actions `31451515654`; no pending core apps |
| Backups | | X | | | latest nightly PASS; restore not run |
| DSA | | | X | | no enrolled live smoke path |
| Equipment PC | | | X | | no controlled PC/booking smoke |
| Workspace | | | X | | depends on Equipment/DSA smoke |
| Results | X | | | | code/tests; live edge NV |
| S3 | | | X | | no safe credentials |
| Android Release | X | | | | assembleRelease + prod API |
| Android Signed APK | | | X | | no keystore |
| Notifications | X | | | | AI.9 deep links + email/in-app |
| Remote Analysis | | X | | | health ready; R9 live NQ |
| Security | X | | | | flags + 43 tests |
| Monitoring | | X | | | log sample; 0 5xx in window |
| Copilot | X | | | | disabled; app not installed on prod |
| FCM | | | X | | credentials absent |

---

## Testing counts (AI.11)

| Suite | Result |
|-------|--------|
| Backend pytest (notifications, push, S3 unit, Copilot, completion, security) | **43 passed** |
| Android `test` + `assembleRelease` | **SUCCESS** (unsigned) |
| Production workflows | showmigrations **success**; observability **success** |

---

## Git / ops artifacts

| Item | Ref |
|------|-----|
| Read-only workflows on `master` | PR #33, #34 |
| Feature branch tip (docs + sync) | see commit after this report |
| Android / Frontend | unchanged (`233740a` / `86cb60d`) |
| DSA / RAA repos | unchanged |

### Exact ops commands still required for broader promotion

```bash
# On EC2 (already wrapped by Actions):
docker exec iic-booking-backend-django-1 python manage.py showmigrations

# Backup restore verify (non-destructive integrity preferred first):
/home/ubuntu/bin/iic-restore-verify.sh /home/ubuntu/backups/nightly/latest

# Lab: DSA + Equipment PC controlled smoke per AI.10 runbook
# Mobile: configure release signingConfigs + build signed APK (do not commit keystore)
```
