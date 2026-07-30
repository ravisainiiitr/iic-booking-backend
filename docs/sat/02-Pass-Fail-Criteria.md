# 02 — Pass / Fail Criteria

## 1. Case-level

| Result | Definition |
|--------|------------|
| **PASS** | Observed behavior matches Expected (API + DB + FS + logs) for that case; evidence attached |
| **FAIL** | Any mismatch, crash, data loss, silent corruption, or security bypass |
| **N/A** | Capability not in this release scope; written justification + approver |

## 2. Severity (defects)

| Sev | Definition | SAT impact |
|-----|------------|------------|
| **S1** | Data loss, security bypass, cannot register/heartbeat, cannot complete E2E sync | **SAT blocked** |
| **S2** | Major feature broken (collect/cleanup/checksum), wrong AVAILABLE state after clean | **SAT blocked** until fixed or waived by SAT lead + security |
| **S3** | Workaround exists (UI polish, non-critical log noise) | May proceed with waiver |
| **S4** | Cosmetic / docs | No block |

## 3. Suite-level PASS

A suite PASSes when:

- All **A** cases in that suite PASS on RC commit, and
- All **L** cases for that suite PASS or approved N/A, and
- No open S1/S2 for that suite.

## 4. System-level PASS (SAT complete)

- Suites SAT-01 … SAT-10 all PASS (or N/A only where documented).
- Automated: `pytest iic_booking/remote_analysis/tests/sat -m sat` green.
- Lab: SAT-05.01–05.11 live path green once.
- Perf: baselines recorded in [08-Performance-Baseline.md](08-Performance-Baseline.md) (values may be env-specific; must exist).
- Production Readiness Report sign-off completed.

## 5. Hard fails (always FAIL)

- Anonymous access to manage/commissioning JSON without credentials succeeds.
- Checksum mismatch marked `UploadVerified`.
- Workstation left `BUSY`/`PREPARING` forever after successful CLEAN with no error flag.
- Agent accepts commands with invalid/expired token.
- Portal stores plaintext agent secrets beyond one-time issuance policy.
- Orphan `RemoteCommand` PENDING forever without expiry after cleanup (per product policy).

## 6. Waivers

Waivers require: case ID, reason, risk, expiry date, approver. Store under change ticket. Expired waiver → case reopens as FAIL.
