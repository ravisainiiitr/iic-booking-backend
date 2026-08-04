# Build & Release Automation Review — Phase 2.6

Goal: rebuild from a **clean clone** of a tagged commit. No business-logic changes.

## Portal Backend

| Item | Finding |
|------|---------|
| Prerequisites | Docker + Git; or Python 3.13 + `uv` |
| Build | `compose/production/django/Dockerfile` |
| Env template | `docs/release/rc1/sample.env.production` |
| Local-machine dependency | **Yes today** — Phase 2.5 only in WT/index |

## Portal Frontend

| Item | Finding |
|------|---------|
| Prerequisites | Node 20+, npm |
| Build | `npm ci && npm run build` / production Dockerfile |
| Env | `VITE_API_URL` |
| Local-machine dependency | **Yes** — Lab/Deploy/SAT pages untracked |

## DSA

| Item | Finding |
|------|---------|
| Prerequisites | .NET 8 SDK, Node/npm, PowerShell |
| Publish | `scripts/Publish-DsaInstaller.ps1 -Version <ver>` |
| Local-machine dependency | High if using local `artifacts/` |

## Equipment Wizard

| Item | Finding |
|------|---------|
| Location | Inside DSA (`EquipmentPcConfigurationWizard`) |
| Gap | Need dedicated publish script documentation |

## RAA

| Item | Finding |
|------|---------|
| Prerequisites | .NET 8 SDK |
| Publish script | **Missing** — add when approved |
| Local-machine dependency | Full source untracked; local DB files (cleaned in 2.7) |

## Must not require

- Pre-built DLLs in clone
- Absolute paths to sibling repos
- Manual copy from `artifacts/` without SHA verification
