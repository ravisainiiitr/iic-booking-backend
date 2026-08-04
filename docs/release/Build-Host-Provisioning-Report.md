# Build Host Provisioning Report — Phase D

**Host under audit:** `RAVI` (Windows 11 Pro)  
**Operator context:** user `Ravi` (non-elevated)  
**Date:** 2026-08-04  
**Scope:** Dedicated Build Host provisioning only  
**Production EC2:** not touched  

---

## PART 1 — Pre-Provision Audit

| Prerequisite | Required | Observed | Result |
|---|---|---|---|
| OS | Windows Server 2022 **or** Windows 11 Pro | Windows 11 Pro 10.0.26200 64-bit | **PASS** |
| Administrator access | Required for Docker / VS Build Tools / features | `IsAdmin=False` | **FAIL (mandatory)** |
| CPU | ≥ 4 vCPU (target 8) | Intel Core Ultra 9 285HX, 24 logical | **PASS** |
| RAM | ≥ 16 GB (target 32) | ~127 GB visible | **PASS** |
| Disk free | ≥ 128 GB usable | C: ~1402 GB free; D: ~1738 GB free | **PASS** |
| Virtualization | Enabled | Hypervisor detected; VBS Running | **PASS** |
| Hyper-V feature state | Queryable / available | Cannot query optional features without elevation | **FAIL (blocked by admin)** |
| WSL2 | Available | Default version = 2 | **PASS (engine)** |
| WSL distro installed | Required for Docker Desktop Linux engine | **No distributions installed** | **FAIL (mandatory)** |
| Docker Desktop compatibility | Installable on host | Docker Desktop **not installed**; path missing | **FAIL (mandatory)** |
| Network → GitHub | Required | HTTP 200 | **PASS** |
| Network → NuGet | Required | HTTP 200 | **PASS** |
| Network → npm | Required | HTTP 200 | **PASS** |
| Network → AWS | Required | HTTP 200 | **PASS** |
| Git | Present or installable | 2.53.0 | **PASS** |
| .NET SDK | 8.x preferred | SDK **10.0.302** present (8.x not listed) | **WARN** |
| Node.js | 20 LTS preferred | **v24.13.1** | **WARN** |
| npm | Present | 11.8.0 | **PASS** |
| PowerShell 7 (`pwsh`) | Required | **Missing** | **FAIL (mandatory for bootstrap scripts)** |
| AWS CLI | Present or installable | 2.33.27 | **PASS** |
| GitHub CLI | Optional | 2.96.0 | **PASS (optional)** |
| winget | Helpful for installs | Present | **PASS** |

### Part 1 decision

**STOP — mandatory prerequisites failed.**

Provisioning Parts 2–6 were **not executed** because:

1. Session is **not elevated** (Administrator required).  
2. **Docker Desktop** is not installed.  
3. **No WSL distribution** is installed (Docker Desktop Linux engine dependency).  
4. **PowerShell 7** (`pwsh`) is missing.

Hardware, disk, network, and base toolchain connectivity are otherwise adequate.

---

## PART 2 — Provision Required Software

**Status:** **NOT EXECUTED** (stopped after Part 1).

Planned (when elevated session is available), via `scripts/build-host/`:

1. `Install-PowerShell7.ps1`  
2. `Install-Git.ps1` (already present)  
3. `Install-DotNetSdk.ps1` (install/retain SDK 8.x alongside 10 if needed)  
4. `Install-NodeJs.ps1` (prefer Node 20 LTS side-by-side or replace 24 for release builds)  
5. Enable WSL2 + install Ubuntu LTS distro  
6. `Install-DockerDesktop.ps1` + start Desktop + Linux engine  
7. `Install-VSBuildTools.ps1`  
8. `Install-AwsCli.ps1` (already present)  
9. `Configure-Docker.ps1` (BuildKit)  

---

## PART 3 — Directory Structure

**Status:** **NOT EXECUTED** (Part 1 stop).

Target layout (unchanged from design):

```text
C:\iic-build\
  repos\
  artifacts\
  logs\
  runners\
  tools\
```

---

## PART 4 — Runner Preparation

**Status:** **NOT EXECUTED** / **NOT REGISTERED** (per Phase D rules and Part 1 stop).

Expected later:

- Binaries under `C:\iic-build\runners\actions-runner`  
- Labels: `self-hosted`, `windows`, `iic-build`  
- No `config.cmd` registration in this phase  

---

## PART 5 — Docker Verification

| Check | Result |
|---|---|
| `docker version` | **FAIL** — Docker CLI not installed |
| `docker compose version` | **FAIL** |
| BuildKit | **N/A** |
| Docker Desktop running | **FAIL** |
| Linux engine | **FAIL** |
| hello-world pull | **NOT ATTEMPTED** |

---

## PART 6 — Release Infrastructure Verification

| Script | Result |
|---|---|
| `Verify-BuildHost.ps1` | **NOT RUN** (would fail: docker/pwsh/VS) |
| `Verify-ReleaseInfrastructure.ps1` | **NOT RUN** |

---

## Installed software snapshot (pre-provision)

| Software | Version / note |
|---|---|
| Windows | 11 Pro 10.0.26200 |
| Git | 2.53.0.windows.1 |
| .NET SDK | 10.0.302 (SDK 8.x not listed) |
| Node | v24.13.1 |
| npm | 11.8.0 |
| AWS CLI | 2.33.27 |
| GitHub CLI | 2.96.0 |
| PowerShell 7 | **Not installed** |
| Docker Desktop | **Not installed** |
| WSL distro | **None** |

### Resources

| Resource | Value |
|---|---|
| CPU | 24 logical processors (Ultra 9 285HX) |
| RAM | ~127 GB |
| C: free | ~1402 GB |
| D: free | ~1738 GB |

---

## Remaining manual actions (before re-run Phase D)

1. **Open an elevated PowerShell (Run as Administrator)** on this host (or provision a dedicated Windows Server 2022 Build Host with admin).  
2. Install **PowerShell 7**.  
3. Install a **WSL2 distro** (e.g. `wsl --install -d Ubuntu-22.04`) and reboot if prompted.  
4. Install **Docker Desktop**, enable **Linux containers**, confirm `docker version` works.  
5. Install **VS 2022 Build Tools** with .NET desktop build tools.  
6. Install/confirm **.NET SDK 8.x** for agent publish alignment.  
7. Prefer **Node 20 LTS** for release builds (or document Node 24 waiver).  
8. Re-run Phase D Parts 2–6 from elevated session using `scripts/build-host\Bootstrap-BuildHost.ps1` (**without** `-RegisterRunner`).  
9. Re-run `Verify-BuildHost.ps1` until **VERIFY_OK**.

---

## PART 8 — Readiness Decision

# NOT READY

### Blockers

| Blocker | Class |
|---|---|
| No Administrator elevation in this session | **Mandatory** |
| Docker Desktop not installed / not running | **Mandatory** |
| No WSL distribution for Docker Linux engine | **Mandatory** |
| PowerShell 7 (`pwsh`) missing | **Mandatory** |
| Hyper-V optional-feature state not confirmable without elevation | **Mandatory** (until verified elevated) |
| .NET SDK 8.x not present (only 10.x) | **Recommended** before agent installer builds |
| Node 20 LTS not present (Node 24 installed) | **Recommended** for release reproducibility |

---

## Explicit non-actions (this phase)

- GitHub Runner **not** registered  
- GitHub Environments **not** created  
- AWS resources **not** configured  
- Docker images **not** built  
- Workflows **not** executed  
- Installers **not** published  
- Production EC2 **not** touched  
- Application source **not** modified  

---

## STOP

Await elevated Build Host access (or a dedicated provisioned host), then re-authorize Phase D Parts 2–6 before Runner Registration.
