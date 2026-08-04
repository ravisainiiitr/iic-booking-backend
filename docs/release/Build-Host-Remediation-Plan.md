# Build Host Remediation Plan — Phase E.1

**Host:** `RAVI` (Windows 11 Pro)  
**Baseline gate:** Phase E `Verify-BuildHostReady.ps1` → **RESULT=FAIL** (5 failures, 8 warnings)  
**Re-check (E.1):** Same **RESULT=FAIL** — no environment changes applied by automation (elevation not attempted).

**Policy:** Operator remediates in an elevated session. Agent does not auto-elevate, install, register runners, or configure AWS.

---

## Root cause analysis — FAILED prerequisites

### 1. Administrator elevation

| Field | Detail |
|---|---|
| **Current** | `IsAdmin=False`; PowerShell 5.1.26100 session not elevated |
| **Expected** | Elevated Administrator session for installs / feature enablement / Docker Desktop |
| **Root cause** | Cursor/agent shell and default user session launch without “Run as administrator” |
| **Remediation** | See Part 2 below — relaunch elevated PowerShell manually |
| **Verify** | `([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')` → `True` |

### 2. PowerShell 7 (`pwsh`)

| Field | Detail |
|---|---|
| **Current** | `pwsh` not on PATH |
| **Expected** | PowerShell **7.4+** available as `pwsh` |
| **Root cause** | Never installed on this host (Windows PowerShell 5.1 only) |
| **Remediation** | Part 3 — winget / MSI install as Administrator |
| **Verify** | `pwsh -NoLogo -Command "$PSVersionTable.PSVersion"` → Major ≥ 7 |

### 3. .NET SDK 8

| Field | Detail |
|---|---|
| **Current** | `dotnet --list-sdks` → only `10.0.302` |
| **Expected** | At least one **8.x** SDK line (agent publish / release scripts target SDK 8) |
| **Root cause** | Only .NET 10 SDK installed; SDK 8 not side-by-side |
| **Remediation** | Part 4 — install .NET SDK 8.x (keep 10.x if desired) |
| **Verify** | `dotnet --list-sdks` contains `8.` |

### 4. Docker Desktop / CLI / Engine / Compose

| Field | Detail |
|---|---|
| **Current** | `docker` missing; `Docker Desktop.exe` path absent |
| **Expected** | Docker CLI + running Linux engine + Compose v2 + BuildKit |
| **Root cause** | Docker Desktop never installed; blocked further by missing Ubuntu WSL distro |
| **Remediation** | Part 6 — install Docker Desktop after WSL Ubuntu (Part 5) |
| **Verify** | `docker version` (Server section present); `docker compose version` |

### 5. Ubuntu WSL distribution

| Field | Detail |
|---|---|
| **Current** | WSL default version 2, but **no distributions** installed |
| **Expected** | Ubuntu 22.04 or 24.04 listed under `wsl -l -v` with VERSION **2** |
| **Root cause** | WSL platform present; distro install step never completed |
| **Remediation** | Part 5 — `wsl --install -d Ubuntu-22.04` (elevated; reboot if prompted) |
| **Verify** | `wsl -l -v` shows Ubuntu, VERSION 2 |

---

## Warnings (recommended before RC1 quality gate)

| Item | Current | Remediation | Verify |
|---|---|---|---|
| Node 20 LTS | Node **v24.13.1** | Install Node 20 LTS | `node -v` → `v20.…` |
| BuildKit | Not set | `[Environment]::SetEnvironmentVariable('DOCKER_BUILDKIT','1','Machine')` after Docker | `$env:DOCKER_BUILDKIT` / machine env = 1 |
| `C:\iic-build\*` | Missing | Elevated: `.\scripts\build-host\Initialize-BuildDirectories.ps1` | `Test-Path C:\iic-build\runners` |

---

## PART 2 — Administrator elevation (manual only)

**Do not attempt elevation from automation.**

### Steps (Windows 11)

1. Close the non-elevated terminal used for failed checks.  
2. Start menu → type **Windows PowerShell** or **PowerShell 7** (after install).  
3. Right-click → **Run as administrator** → Yes on UAC.  
4. Confirm:

```powershell
whoami /groups | findstr /i "S-1-5-32-544"
# Or:
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

Expected: Administrators group present / `True`.

5. `cd` to the backend repo and continue Parts 3–6 in **that** window.

---

## PART 3 — PowerShell 7

| Item | Value |
|---|---|
| Official source | https://aka.ms/powershell-release?tag=stable |
| winget | `winget install --id Microsoft.PowerShell -e --accept-source-agreements --accept-package-agreements` |
| MSI | Download `PowerShell-7.x.x-win-x64.msi` from GitHub Releases |
| Silent | `msiexec /i PowerShell-7.x.x-win-x64.msi /qn` |
| Verify | `pwsh -NoLogo -Command "$PSVersionTable.PSVersion"` |
| Expected | `7.4.x` (or newer 7.x) |

---

## PART 4 — .NET SDK 8

| Item | Value |
|---|---|
| Official source | https://dotnet.microsoft.com/download/dotnet/8.0 |
| winget | `winget install --id Microsoft.DotNet.SDK.8 -e --accept-source-agreements --accept-package-agreements` |
| Verify | `dotnet --list-sdks` |
| Expected | Line like `8.0.xxx [...]` (10.x may still appear) |

---

## PART 5 — WSL + Ubuntu

| Check | Command | Expected |
|---|---|---|
| Status | `wsl --status` | Default Version: **2** |
| List | `wsl -l -v` | Ubuntu row, VERSION **2** |

If Ubuntu missing (elevated):

```powershell
wsl --install -d Ubuntu-22.04
# Reboot if Windows requests it, then complete first-time Ubuntu user setup
wsl -l -v
```

---

## PART 6 — Docker Desktop

**Install only after Ubuntu WSL exists.**

| Item | Value |
|---|---|
| Official source | https://www.docker.com/products/docker-desktop/ |
| Installer | `Docker Desktop Installer.exe install --quiet --accept-license` (see current Docker docs) |
| Post-install | Start Docker Desktop → Settings → General: use **WSL 2** engine; enable Ubuntu integration |
| BuildKit | Set machine env `DOCKER_BUILDKIT=1`; restart shells |
| Verify | `docker version` ; `docker compose version` ; `docker info` |
| Expected | Server Version populated; Compose v2 plugin; Linux info |

**Do not** `docker login` to ECR in this phase.

Optional smoke (after engine up; not an app build):

```powershell
docker run --rm hello-world
```

---

## Recommended operator sequence (elevated)

1. Elevate PowerShell (Part 2)  
2. Install PowerShell 7 (Part 3) — open new elevated `pwsh`  
3. Install .NET SDK 8 (Part 4)  
4. Install Ubuntu WSL (Part 5) — reboot if needed  
5. Install Docker Desktop (Part 6) — start engine  
6. `Initialize-BuildDirectories.ps1 -BuildRoot C:\iic-build`  
7. Optional: Node 20 LTS + BuildKit env  
8. Re-run `Verify-BuildHostReady.ps1` until **RESULT=PASS**  
9. Return to **Phase E** (runner registration still requires separate approval)

---

## Verification loop policy

After **each** major install, re-run:

```powershell
cd D:\IIC_NEW\iic-booking-backend-rt-port
pwsh -File .\scripts\build-host\Verify-BuildHostReady.ps1
```

Record PASS/FAIL deltas in [Build-Host-Remediation-Completion.md](Build-Host-Remediation-Completion.md).

**Automation did not perform installs in E.1** (no elevation). Loop awaits operator actions.
