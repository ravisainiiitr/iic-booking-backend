# AI.10 — Limited Production Pilot Readiness

**Date:** 2026-08-11  
**Mode:** AUTO continuous execution  
**Decision:** **READY FOR LIMITED PRODUCTION PILOT**

---

## 1. AI.9 baseline

| Repo | Branch | SHA (AI.9 end) |
|------|--------|----------------|
| Backend | `feature/ai-copilot-android` | `be81944` |
| Android | `master` | `233740a` |
| Frontend | `feature/r6-remote-analysis-software-centric` | `86cb60d` |

AI.9 report: `docs/release/phase-AI/AI.9-Staged-Pilot-Qualification-Report.md`

Inherited:

- CORE: READY FOR LIMITED PRODUCTION PILOT  
- COPILOT: NOT READY (`RESEARCH_COPILOT_ENABLED=false`)  
- FCM: BLOCKED  
- STAGING: BLOCKED (no staging compose)  
- Live S3 E2E: BLOCKED  

AI.10 did **not** redesign architecture, invent staging, enable Copilot/FCM, or modify production data.

---

## 2. Production inventory

| Item | Status |
|------|--------|
| Production portal | https://equip.iitr.ac.in — **configured / reachable** |
| Backend deploy | GitHub Actions + `docker-compose.production.yml` — **configured** |
| Frontend | Production home HTTP 200 — **healthy** |
| Staging compose | **not configured** (absent by design for AI.10) |
| Local Django (dev) | Up (compose local) — qualification only |
| Android SHA used | `233740a` |
| Frontend SHA | `86cb60d` |
| Backend tip before AI.10 docs | `be81944` |
| Production `.envs/.production/.django` in this workspace | **not configured** (secrets stay on host) |
| `RESEARCH_COPILOT_ENABLED` code/compose default | **disabled** (`false`) |
| Production capabilities `research_copilot` | **disabled** (`false`) — live probe |
| FCM server key / google-services | **not configured** |
| Sentry DSN in this workspace | **not configured** (production may differ on host) |

Docker image digests / EC2 container list: **NOT VERIFIED** from this workstation (no host SSH in AI.10). Use phase-M daily checks on EC2.

---

## 3. Migration status

| Mechanism | Present |
|-----------|---------|
| Migrate on Django container start (`compose/production/django/start`) | **Yes** |
| `migrate-production.yml` / `docker exec … migrate --noinput` | **Yes** |
| Manual DB edits | **Forbidden** |

| Environment | Applied / Pending / Unknown |
|-------------|------------------------------|
| Local test DB (pytest) | Applied as needed by tests |
| Production RDS | **Unknown** from this workstation |

**Prepared safe command (existing process only):**

```bash
docker compose -f docker-compose.production.yml exec django python manage.py migrate --noinput
```

AI.10 did **not** apply production migrations remotely.

**Migrations: PARTIAL** (process PASS; live applied-set Unknown until EC2 `showmigrations`)

---

## 4. API health

Live probes against production (2026-08-11):

| Endpoint | HTTP |
|----------|------|
| `/api/version/` | **200** |
| `/api/v1/provisioning/capabilities/` | **200** (`research_copilot=false`) |
| `/api/v1/analysis/health/live/` | **200** |
| `/api/v1/analysis/health/ready/` | **200** (db/cache ok; tunnel secrets reported configured) |
| `/api/v1/analysis/health/` | **200** |
| Portal `/` | **200** |

No duplicate health APIs created.

**API health: PASS**

---

## 5. Booking

Automated regression retained (completion / results-available / Copilot auth). No automatic real production bookings created.

AI.7–AI.9 booking lifecycle evidence remains the pilot baseline.

Emulator (debug→local): Home shows operator session; My Bookings UI reachable; **no new bookings created** in AI.10.

**Booking: PASS** (prior E2E + regression; no unsafe prod booking automation)

---

## 6. Sample workflow

Code + prior AI.8 evidence:

- Accept / Reject API  
- Reject requires reason  
- Operator Android confirmation dialog  

AI.10 emulator: Operations entry visible for lab incharge session; full live accept/reject not re-driven against production.

**Sample workflow: PASS** (code/regression/prior E2E); live prod operator action = checklist item for staff

---

## 7. Results

Intended path: Complete → upload → storage → metadata → notify → Booking Details download.

- Completion email callers pass **empty attachment list** (verified code + `test_booking_completion_r7`)
- Cross-user download denied (AI.8)
- Temp cleanup after successful S3 publish: `delete_local_upload_copy` (failed upload retains local for retry)

**Email without attachment: PASS**  
**Results workflow (code): PASS**  
**Live S3 E2E: BLOCKED** (no safe non-prod credentials; not uploaded to production)

---

## 8. Notifications

| Event | Payload / deep link readiness |
|-------|-------------------------------|
| Booking Confirmed | booking_events metadata includes `real_booking_id` |
| Booking Cancelled | same |
| Sample Rejected | typically via refund/cancel notification path with metadata |
| Booking Completed | completion email + events as configured |
| Result Available | explicit `real_booking_id` on push/email metadata |
| Sample Accepted | **no dedicated user push found** in AI.10 inventory — lifecycle recorded; treat as **PARTIAL** for dedicated accept alert |

AI.9 Android deep links: API `real_booking_id` → Booking Detail; auth-scoped virtual resolve; graceful fallback.

Authorization remains server-side (recipient-scoped notification list; booking list scoped).

**Notifications: PASS** for deep-link plumbing + core events; Sample Accepted dedicated alert = PARTIAL

---

## 9. Android

Commands on SHA `233740a`:

```text
.\gradlew.bat clean
.\gradlew.bat test
.\gradlew.bat assembleRelease
```

**BUILD SUCCESSFUL**

Release BuildConfig:

- `API_BASE_URL=https://equip.iitr.ac.in/api/`
- `API_ENVIRONMENT=production`
- `DEBUG=false`

APK scan: `10.0.2.2` = 0, `127.0.0.1` = 0; production host present.

Emulator smoke (debug → local API):

- Home (logged in)  
- My Bookings  
- Alerts  
- Profile (shows local API base as expected for **debug**)  
- Force-stop → reopen → session retained  

Release APK remains **unsigned** (ops signing). No production bookings from AI.10.

**Android Release: PASS**  
**Android user workflow: PASS** (debug/local smoke + prior AI.8 persistence/401)  
**Android release-on-device prod API: PARTIAL** (unsigned; not forced onto prod accounts)

---

## 10. DSA

Architecture unchanged: Equipment PC LAN-only; DSA bridges.

AI.10: no DSA code changes; no live DSA host verification from this workstation.

**DSA: PARTIAL** (architecture/docs PASS; live host NOT VERIFIED here)

---

## 11. Equipment PC

No Internet dependency introduced. No AI.10 changes to Equipment PC installer path.

**Equipment PC: PARTIAL** (design PASS; live bench NOT VERIFIED here)

---

## 12. RAA

Production readiness probe reports remote_analysis ready (db/cache/transport configured).

AI.10: no RAA redesign; no agent process inspection on Windows hosts.

**RAA: PARTIAL** (API readiness PASS; agent host process NOT VERIFIED here)

---

## 13. Remote Analysis / software-centric

Scheduler remains capability/software based (department, online, healthy, software, license, resources, load, LRU). No reintroduction of Equipment→Remote PC hard map in AI.10.

R9 Data Workspace live qualification: **NOT QUALIFIED / PARTIAL** (not re-proven in AI.10; do not claim PASS from source alone).

Frontend production build (`npm run build`) **PASS** on `86cb60d` tree (unrelated dirty files not committed).

**Remote Analysis: PARTIAL**

---

## 14. Copilot

- Code default: disabled  
- Compose default: false  
- Live capabilities: `research_copilot=false`  
- AI.10 did not enable Copilot  
- Authorization tests included in 34-pass suite  

**Copilot: NOT READY / safely disabled — PASS for pilot safety**

---

## 15. FCM

**FCM = BLOCKED BY CREDENTIALS**

Supported production path for pilot: **email + in-app notifications**.

---

## 16. Monitoring

| Check | Result |
|-------|--------|
| Public health endpoints | PASS (200) |
| EC2 docker/celery/disk live review | NOT VERIFIED (no SSH) |
| Log secret dump review on prod | NOT VERIFIED |
| Code logging of completion/results | uses booking ids; do not log secrets |

Recurring prod 5xx scan: not performed via log dump (would require host access). Ops should follow phase-M daily curls.

**Monitoring: PARTIAL**

---

## 17. Backup

Documented (not destructively tested):

- Nightly backup scripts + paths in phase-M Operations Runbook  
- `scripts/deploy/backup.sh`, `scripts/ops/iic-nightly-backup.sh`  
- Restore-verify scripts exist  

**Backup: NOT VERIFIED** (process documented; live nightly success not confirmed from this workstation)

---

## 18. Rollback

| Item | Value |
|------|-------|
| Deployment method | GitHub Actions self-hosted + compose production |
| Current release identity | git tag / `.deploy-state/current_*` on host |
| Previous release | `.deploy-state/previous_*` |
| Auto rollback | Deploy workflow on health/smoke failure |
| Manual | `scripts/deploy/rollback.sh` [`ROLLBACK_REF=<sha>`] |

Rollback **not executed** as a test.

**Rollback: PASS** (documented + scripted)

---

## 19. Pilot runbook

Created:

- `docs/release/phase-AI/AI.10-Limited-Production-Pilot-Runbook.md`
- `docs/release/phase-AI/AI.10-Pilot-Support-Matrix.md`
- `docs/release/phase-AI/AI.10-Pilot-Checklist.md`

---

## 20. Remaining blockers

1. Live S3 E2E without safe non-prod credentials  
2. FCM credentials / Android Firebase packaging  
3. Production Copilot (intentionally OFF)  
4. Staging environment (intentionally not invented)  
5. Host-side migration `showmigrations`, backup verify, docker inventory  
6. Signed Play/store Android artifact  
7. R9 Data Workspace live qualification  

---

## 21. Final recommendation

| Area | Recommendation |
|------|----------------|
| **CORE PLATFORM** | **READY FOR LIMITED PRODUCTION PILOT** |
| **COPILOT** | **NOT READY** — keep disabled |
| **FCM** | **BLOCKED BY CREDENTIALS** |
| **S3** | **PARTIAL / BLOCKED** for live E2E; failure handling + local path OK |
| **ANDROID** | Release config **READY**; signing/pilot cohort install = ops |
| **RA / DSA edge** | Proceed with lab checklist; treat live edge as PARTIAL until host verified |

### Final decision: **READY FOR LIMITED PRODUCTION PILOT**

Do **not** promote to broader production until checklist host items (migrations showmigrations, backup verify, DSA/Equipment smoke) are signed by ops/lab.

---

## Status table

| Area | PASS | PARTIAL | BLOCKED | Evidence |
|------|------|---------|---------|----------|
| Production Backend | X | | | health/ready + version 200 |
| Production Frontend | X | | | portal home 200; `npm run build` |
| Migrations | | X | | start/migrate process PASS; prod applied-set Unknown |
| Booking | X | | | AI.7–9 E2E + regression; no unsafe prod booking |
| Sample Workflow | X | | | AI.8 API/UI + code path |
| Results | X | | | download/authz + completion path |
| S3 | | | X | live E2E blocked by credentials |
| Email Without Attachment | X | | | callers `[]` + tests |
| Notifications | X | | | deep links + metadata; Sample Accepted alert soft gap |
| Android Release | X | | | assembleRelease + BuildConfig + APK scan |
| DSA | | X | | architecture OK; host not verified here |
| Equipment PC | | X | | no Internet dependency; host not verified |
| RAA | | X | | API ready; agent host not verified |
| Remote Analysis | | X | | software-centric retained; R9 live NOT QUALIFIED |
| Copilot | X | | | disabled in prod capabilities + defaults |
| FCM | | | X | credentials absent |
| Monitoring | | X | | public probes PASS; host logs NOT VERIFIED |
| Backup | | X | | documented; live verify NOT VERIFIED |
| Rollback | X | | | scripts + deploy auto-rollback docs |

---

## Testing counts

| Suite | Result |
|-------|--------|
| Backend targeted pytest | **34 passed** |
| Android `test` + `assembleRelease` | **SUCCESS** |
| Frontend `npm run build` | **SUCCESS** |

---

## Git SHAs (post AI.10)

| Repo | Branch | SHA |
|------|--------|-----|
| Backend | `feature/ai-copilot-android` | `9026580` |
| Android | `master` | `233740a` (unchanged) |
| Frontend | `feature/r6-remote-analysis-software-centric` | `86cb60d` (unchanged) |
| DSA / RAA | — | unchanged in AI.10 |

