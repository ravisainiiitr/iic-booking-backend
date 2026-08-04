# Build Verification Checklist — Platform RC1

Use this checklist for **every** artifact build run.  
Initial all boxes. Any **FAIL** stops the pipeline.

**Platform version:** `2.5.0-rc1`  
**Build host:** ______________________  
**Operator:** ______________________  
**Date (IST):** ______________________

---

## 0. Build host gate

| # | Check | PASS / FAIL |
|---|---|---|
| 0.1 | Host is **not** production EC2 | |
| 0.2 | `docker version` succeeds | |
| 0.3 | `docker compose version` succeeds | |
| 0.4 | `dotnet --list-sdks` shows required SDK | |
| 0.5 | `node -v` / `npm -v` OK | |
| 0.6 | VS Build Tools / MSBuild available (Windows installers) | |
| 0.7 | Disk free ≥ 40 GB on Docker + artifact volumes | |

---

## 1. Per-repository source gate

Repeat for Backend, Frontend, DSA, RAA.

| # | Check | Backend | Frontend | DSA | RAA |
|---|---|---|---|---|---|
| 1.1 | `git fetch --tags` | | | | |
| 1.2 | Checkout **approved tag only** (not branch) | `v2.5.0-rc1` | `v2.5.0-rc1` | `v1.0.0-rc1` | `v1.0.0-rc1` |
| 1.3 | `git rev-parse HEAD` matches freeze certificate | `c512199…` | `e548c79…` | `495e27b…` | `170d689…` |
| 1.4 | Clean checkout — `git status --porcelain` empty for tracked paths | | | | |
| 1.5 | No unpublished / detached wrong commit | | | | |

SHA mismatch → **STOP**.

---

## 2. Backend images

| # | Check | PASS / FAIL | Evidence |
|---|---|---|---|
| 2.1 | Successful Docker build (django, celeryworker, celerybeat, flower) | | log path |
| 2.2 | Images tagged `*:2.5.0-rc1` | | |
| 2.3 | Image IDs recorded | | |
| 2.4 | Digests / IDs recorded in Docker-Image-Manifest | | |
| 2.5 | Build warnings reviewed (none critical) | | |

---

## 3. Frontend image

| # | Check | PASS / FAIL | Evidence |
|---|---|---|---|
| 3.1 | Successful Docker build | | |
| 3.2 | Tag `iic_booking_production_frontend:2.5.0-rc1` | | |
| 3.3 | Image ID + digest/ID recorded | | |
| 3.4 | Bundle / static size summary recorded | | |

---

## 4. DSA installer

| # | Check | PASS / FAIL | Evidence |
|---|---|---|---|
| 4.1 | `Publish-DsaInstaller.ps1` Release succeeds | | |
| 4.2 | Setup EXE present | | path + size |
| 4.3 | Version stamped `1.0.0-rc1` | | |
| 4.4 | SHA256 generated | | |

---

## 5. Equipment Wizard

| # | Check | PASS / FAIL | Evidence |
|---|---|---|---|
| 5.1 | `dotnet publish` Release succeeds | | |
| 5.2 | Wizard EXE present | | path + size |
| 5.3 | Version `1.0.0-rc1` | | |
| 5.4 | SHA256 generated | | |

---

## 6. RAA installer

| # | Check | PASS / FAIL | Evidence |
|---|---|---|---|
| 6.1 | `Publish-Installer.ps1` succeeds | | |
| 6.2 | `RemoteAnalysisAgentSetup.exe` present | | |
| 6.3 | Version arg `1.0.0-rc1` (legacy VERSION noted) | | |
| 6.4 | SHA256 generated | | |

---

## 7. Checksums & manifests

| # | Check | PASS / FAIL |
|---|---|---|
| 7.1 | `ArtifactChecksums-SHA256.txt` complete | |
| 7.2 | Re-hash spot-check matches file | |
| 7.3 | `Docker-Image-Manifest.md` complete | |
| 7.4 | `Installer-Manifest.md` complete | |
| 7.5 | `Platform-RC1-Artifact-Manifest.md` complete | |
| 7.6 | Compatibility matrix filled | |

---

## 8. Local artifact qualification (no production)

| # | Check | PASS / FAIL |
|---|---|---|
| 8.1 | Backend image smoke (entrypoint / import) | |
| 8.2 | Frontend image serves static content | |
| 8.3 | DSA installer launches | |
| 8.4 | Wizard launches | |
| 8.5 | RAA installer launches | |

---

## 9. Negative controls

| # | Check | PASS / FAIL |
|---|---|---|
| 9.1 | No `docker push` performed | |
| 9.2 | No production SSH / compose up on prod | |
| 9.3 | No git commit / tag change | |
| 9.4 | No Deployment Center upload (Batch 7 only) | |

---

## Sign-off

| Decision | Circle one |
|---|---|
| **Artifact Ready** | YES / NO |
| **Artifact Failed** | YES / NO |

Failure artifact / step / root cause: _________________________________

Operator signature: __________________  Date: __________
