# Build Host Commissioning Report — Phase E

**Date:** 2026-08-04  
**Host:** `RAVI` (Windows 11 Pro)  
**Gate script:** `scripts/build-host/Verify-BuildHostReady.ps1`  
**Gate result:** **RESULT=FAIL** (failures=5, warnings=8)  
**Decision:** **NOT READY**

---

## PART 1 — Commissioning Verification

Executed via Windows PowerShell 5.1 (`pwsh` not available).

| Check | Result | Detail |
|---|---|---|
| Administrator | **FAIL** | Session not elevated |
| PowerShell 7 | **FAIL** | `pwsh` not on PATH |
| Git | PASS | 2.53.0.windows.1 |
| .NET SDK 8 | **FAIL** | Only 10.0.302 present |
| Node.js 20 | WARN | v24.13.1 (want v20 LTS) |
| npm | PASS | 11.8.0 |
| Docker CLI / Engine | **FAIL** | docker not on PATH |
| Docker Compose | **FAIL** (blocked) | Docker missing |
| BuildKit | WARN | DOCKER_BUILDKIT not set |
| WSL2 | PASS | Default version 2 |
| Ubuntu distro | **FAIL** | No Ubuntu distribution |
| AWS CLI | PASS | 2.33.27 |
| GitHub CLI | PASS | 2.96.0 |
| VS Build Tools / MSBuild | PASS | VS 18 Community path via vswhere |
| CPU | PASS | 24 logical |
| RAM | PASS | 127.5 GB |
| Disk C: | PASS | ~1402 GB free |
| `C:\iic-build\...` | WARN | Directories missing |
| Internet (GitHub/NuGet/npm/AWS) | PASS | HTTP 200 |

### Mandatory FAILs (stop conditions)

1. Not running as Administrator  
2. PowerShell 7 not installed  
3. .NET SDK 8.x not installed  
4. Docker Desktop / CLI not installed  
5. Ubuntu WSL distribution not installed  

---

## PARTS 2–6 — Not executed

Per Phase E rule: **If RESULT != PASS → STOP immediately.**

| Part | Status |
|---|---|
| 2 GitHub Runner Registration | **NOT DONE** |
| 3 Docker Verification (hello-world, etc.) | **NOT DONE** |
| 4 GitHub Environment Verification | **NOT DONE** |
| 5 AWS Verification | **NOT DONE** |
| 6 Dry Infrastructure Test workflow | **NOT DONE** |

---

## PART 8 — Decision

# NOT READY

### Blockers

| Blocker | Remediation |
|---|---|
| Administrator elevation | Re-run commissioning from elevated PowerShell |
| PowerShell 7 missing | `winget install Microsoft.PowerShell` (elevated) |
| .NET SDK 8 missing | Install SDK 8.x alongside 10.x |
| Docker missing | Install Docker Desktop + Linux/WSL2 backend |
| Ubuntu WSL missing | `wsl --install -d Ubuntu-22.04` then reboot if needed |
| `C:\iic-build` missing | After elevation: `Initialize-BuildDirectories.ps1` |

### Recommended (non-blocking for gate, blocking for RC1 quality)

| Item | Remediation |
|---|---|
| Node 20 LTS | Install Node 20 (currently Node 24) |
| DOCKER_BUILDKIT=1 | Set machine environment after Docker install |

---

## Remaining operator work before re-entering Phase E

1. Complete elevated installs from `docs/release/Build-Host-Installation-Guide.md`.  
2. Create `C:\iic-build` layout.  
3. Re-run `Verify-BuildHostReady.ps1` until **RESULT=PASS**.  
4. Re-authorize Phase E Parts 2–6 (runner registration requires a GitHub registration token).  

---

## Explicit non-actions this phase

- No runner registration  
- No GitHub Environment creation  
- No AWS mutation  
- No release workflows  
- No image builds  
- No deploy  
- No application source changes  

---

## STOP

Await operator remediation and **RESULT=PASS** before Runner Registration or Phase F.
