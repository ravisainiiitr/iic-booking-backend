# Installer Validation — DSA / RAA / Equipment Wizard

**Status:** Checklist for RC1 lab verification. Values marked TBD until measured.

---

## Common requirements (all installers)

| Check | DSA | RAA | Wizard | Evidence |
|-------|-----|-----|--------|----------|
| Version string matches Manifest | TBD | TBD | TBD | |
| Publisher / CompanyName metadata | TBD | TBD | TBD | |
| SHA-256 published in Deployment Center | TBD | TBD | TBD | |
| Digital signature (Authenticode) | TBD / waiver | TBD / waiver | TBD / waiver | |
| Upgrade over prior version | TBD | TBD | TBD | |
| Repair path | TBD | TBD | TBD | |
| Uninstall clean | TBD | TBD | TBD | |
| Silent / unattended install | TBD | TBD | TBD | |
| No secrets in plaintext logs | TBD | TBD | TBD | |

---

## DSA (`Publish-DsaInstaller.ps1`)

| Topic | Finding / action |
|-------|------------------|
| Build path | Documented: frontend build → self-contained API → WPF setup EXE + SHA256 + offline ZIP |
| Output | Must not commit `artifacts/`; publish to portal via `publish_dsa_installer` |
| Silent install | Verify flags in installer UX/docs during SAT-DEP-001 |
| ManagementApiKey | Fail-closed pairing (Phase 2.5 fix) — validate SAT-SEC-001 |
| Upgrade | Preserve enrollment/token under ProgramData |

## Equipment PC Wizard

| Topic | Finding / action |
|-------|------------------|
| Distribution | Portal Deployment Center + `publish_equipment_wizard` |
| Pairing | Requires DSA discovery + pairing token |
| OTP | Must not persist in DSA ConfigJson (H-02) — SAT-COM-003 |
| Repair | Prefer re-run wizard / repair package fields on release model |

## RAA

| Topic | Finding / action |
|-------|------------------|
| Packaging | **Missing standardized publish script** in repo — block RC agent GA until added |
| Enrollment | `X-Enrollment-Key` / portal enrollment |
| Update discover | Portal endpoints with agent/enrollment auth (WT fix) |
| Uninstall | Document service removal + ProgramData retention policy |

---

## RC policy on signing

If Authenticode is unavailable for RC1:

1. Record **unsigned** in Manifest.  
2. Require SHA-256 verification on every download.  
3. Schedule signing before **GA 2.5.0** (non-negotiable for campus-wide).
