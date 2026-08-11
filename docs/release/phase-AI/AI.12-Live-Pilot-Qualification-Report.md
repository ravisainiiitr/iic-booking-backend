# AI.12 — Live Pilot Qualification Report

**Date:** 2026-08-11  
**Mode:** AUTO continuous execution  
**Decision:** **READY FOR LIMITED PRODUCTION PILOT — EXTENDED**

**Broader production:** **NOT READY**

---

## 1. AI.11 baseline

| Repo | Branch | SHA |
|------|--------|-----|
| Backend | `feature/ai-copilot-android` | `edc1c96` |
| Android | `master` | `233740a` |
| Frontend | `feature/r6-remote-analysis-software-centric` | `86cb60d` |

AI.11: limited pilot extended; broader production blocked on DSA/Equipment live smoke, signed APK, S3 E2E, backup restore.

AI.12 closed **backup restore** with live EC2 evidence. Remaining Gate 2 / S3 / signed APK blockers were **not invented away**.

---

## 2. DSA qualification

| Check | Result |
|-------|--------|
| Local `DepartmentSyncAgent` Windows service | **RUNNING** |
| Local API `GET /api/health` | **200** Healthy; `instrumentedPcCount=28` |
| Enrolled instruments API | **200**; **2** enrolled (XRF@PREDATOR, PXRD [A]@RAVI-PC) |
| Enrolled heartbeat freshness | **STALE** (≈14–15 h old); `fresh_lt_30m=0` |
| `EquipmentPcHeartbeatAgent` on this host | **NOT installed** |
| Controlled online→offline→online cycle | **NOT PERFORMED** (would require lab Equipment PC power/network control) |

**DSA: PARTIAL** — agent service + APIs healthy; live enrollment heartbeat smoke **not** current.

---

## 3. Equipment PC qualification

| Check | Result |
|-------|--------|
| Real Equipment PC agent on this workstation | **ABSENT** |
| Controlled booking for workspace proof | **NOT CREATED** (no authorized new pilot booking invented) |
| Physical `D:\Results\Active\<booking>` proof | **NOT VERIFIED** (`D:\Results` exists on DSA host `RAVI`; Active empty / no booking folder proven) |
| Do not confuse DSA watch vs Equipment PC disk | Documented; DSA-local `D:\Results` alone ≠ Equipment PC workspace PASS |

**Equipment PC: BLOCKED** for AI.12 live smoke.

---

## 4. Workspace qualification

**BLOCKED** — depends on Equipment PC live path + controlled booking.

---

## 5. SMB qualification

**BLOCKED** — no authenticated SMB exercise against Equipment PC share in this session (would require live enrolled PC credentials; not invented).

---

## 6. Offline recovery

**BLOCKED** — no Equipment PC power-cycle test performed.

---

## 7. Result workflow

Code/regression retained (completion email without attachments; authz download).

Live Equipment→portal result path: **NOT VERIFIED** in AI.12.

**Results: PASS** (application path) / live edge **NOT VERIFIED**

---

## 8. S3

Local/runtime: bucket not set; no safe non-prod credentials.

Unit failure handling remains from AI.9.

**S3 LIVE E2E: BLOCKED**

Operational mitigation for limited pilot: portal/app download via configured storage when available; do not email attachments; retry uploads on failure (existing code).

---

## 9. Backup restore

| Step | Evidence |
|------|----------|
| Latest nightly | `nightly-20260810` / `portal.sql.gz` (~16MB) |
| Gzip integrity | **PASS** — Actions `31452276302` |
| Temp DB restore (`VERIFY_RESTORE_DB=1`) | **PASS** — created `iic_restore_verify_*`, restored, dropped — Actions `31452346947` |
| Production DB overwritten? | **No** |

Workflow: `AI12 Backup Integrity Verify` (merged to `master`, PR #35).

**Backup: PASS**  
**Backup Restore: PASS**

---

## 10. Android signing

| Item | Status |
|------|--------|
| Unsigned `assembleRelease` | **PASS** |
| Keystore / `signingConfigs` | **ABSENT** |
| Signed APK | **BLOCKED** (keystore not invented) |
| `applicationId` | `ac.in.iitr.iicbooking` |
| versionCode / versionName | `1` / `1.0.0` |
| Release API | `https://equip.iitr.ac.in/api/` |

---

## 11. Android device test (signed)

**BLOCKED** — no signed APK.

Prior debug/local smoke (AI.10) retained only as historical.

---

## 12. Notifications

Deep links + recipient scoping: PASS (AI.9 + regression).  
FCM remains blocked; email/in-app supported.

**Notifications: PASS**

---

## 13. Remote Analysis

Production `/api/v1/analysis/health/ready/` → **200 ready**.  
R9 Data Workspace live lab qualification: still **NOT QUALIFIED** as E2E (migrations ≠ live PASS).

**Remote Analysis: PARTIAL**

---

## 14. Security

| Check | Result |
|-------|--------|
| Prod `research_copilot=false` | PASS |
| Prod Copilot settings / FCM key | OFF / not configured (AI.11 host probe) |
| Backend suite including security hardening | **43 passed** |
| No invented AWS/Firebase/keystore secrets | PASS |

**Security: PASS** (regression + flag posture)

---

## 15. Monitoring

Observability sample `31452371146`: ready_http=200; monitoring heartbeats; no new 5xx campaign identified in sampled window.

**Monitoring: PARTIAL**

---

## 16. Remaining blockers (broader production)

1. **Equipment Gate 2:** live enrolled Equipment PC heartbeat (fresh), online/offline recovery, workspace on Equipment PC disk, SMB R/W  
2. **Controlled booking** workspace proof (authorized pilot account only)  
3. **Signed Android APK** + device install  
4. **Safe S3 live E2E** (or formal risk acceptance with mitigation)  
5. Copilot remains **NOT READY** (intentional)  
6. FCM remains **BLOCKED** (non-blocking for core)

---

## 17. Broader production recommendation

### Gates

| Gate | Result |
|------|--------|
| 1 CORE APPLICATION | **PASS** (health, migrations AI.11, regression 43, FE prior build, Android unsigned release, booking/sample/results/notifications evidence chain) |
| 2 EQUIPMENT | **FAIL** — DSA partial; Equipment live smoke BLOCKED |
| 3 DATA | **PARTIAL** — results security PASS; S3 live BLOCKED with mitigation notes |
| 4 BACKUP | **PASS** — integrity + temp restore |
| 5 MOBILE | **FAIL for broader** — signed APK BLOCKED (acceptable only while limited pilot uses unsigned/internal sideload policy) |
| 6 AI | **PASS** — Copilot OFF |
| 7 FCM | **PASS** (may remain blocked) |

### Final decision: **READY FOR LIMITED PRODUCTION PILOT — EXTENDED**

Do **not** declare READY FOR BROADER PRODUCTION until Gate 2 (and preferably Gate 5 + S3 policy) close with live lab evidence.

| Layer | Status |
|-------|--------|
| CORE PLATFORM | READY (limited pilot) |
| BACKUP / RESTORE | PASS |
| DSA | PARTIAL |
| EQUIPMENT LIVE | BLOCKED |
| S3 LIVE | BLOCKED |
| SIGNED APK | BLOCKED |
| COPILOT | NOT READY — keep OFF |
| FCM | BLOCKED |

---

## Status table

| Area | PASS | PARTIAL | BLOCKED | NOT VERIFIED | Evidence |
|------|------|---------|---------|--------------|----------|
| Production Health | X | | | | version/ready/capabilities 200 |
| Migrations | X | | | | AI.11 showmigrations |
| DSA | | X | | | service healthy; enrolled HB stale |
| Equipment Enrollment | | X | | | 2 enrolled; HB not fresh |
| Equipment PC | | | X | | no EquipmentPc agent / live smoke |
| Heartbeat | | | X | | enrolled HB ~14h stale; no cycle test |
| Workspace Creation | | | X | | no controlled booking proof |
| SMB | | | X | | not exercised |
| Offline Recovery | | | X | | not exercised |
| Results | X | | | | code/tests; live edge NV |
| S3 | | | X | | no safe credentials |
| Backup | X | | | | nightly dump present |
| Backup Restore | X | | | | gzip + temp DB restore PASS |
| Android Build | X | | | | assembleRelease |
| Signed APK | | | X | | no keystore |
| Android Device | | | X | | needs signed APK |
| Notifications | X | | | | AI.9 + tests |
| Remote Analysis | | X | | | health ready; R9 live NQ |
| Security | X | | | | 43 tests + flags |
| Monitoring | | X | | | sample OK |
| Copilot | X | | | | disabled |
| FCM | | | X | | credentials absent |

---

## Testing counts

| Suite | Result |
|-------|--------|
| Backend pytest (AI suite + security) | **43 passed** |
| Android test + assembleRelease | **SUCCESS** (unsigned) |
| Backup integrity Actions | **PASS** (`31452276302`) |
| Backup temp restore Actions | **PASS** (`31452346947`) |

---

## Required to unlock broader production

1. Lab session: Equipment PC with heartbeat agent → prove ONLINE/OFFLINE/ONLINE + recovery time  
2. One authorized controlled booking → workspace on **Equipment PC** path (not DSA-only) + SMB from DSA  
3. Provide release signing keystore via secure ops channel → signed APK + device smoke  
4. Provide safe S3 test bucket **or** written risk acceptance for storage posture  
5. Keep `RESEARCH_COPILOT_ENABLED=false` until separate AI readiness
