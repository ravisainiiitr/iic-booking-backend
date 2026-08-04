# Build Host Installation Guide

**Audience:** Operator commissioning a dedicated Windows Release Build Host  
**Platform:** IIC Equipment Booking & Remote Analysis  
**Related:** [Build-Host-Offline-Package.md](Build-Host-Offline-Package.md) · [Build-Host-Commissioning-Gate.md](Build-Host-Commissioning-Gate.md) · `scripts/build-host/Verify-BuildHostReady.ps1`

**Rules:** Do not use Production EC2 as the build host. Do not register the GitHub runner until the commissioning gate says READY FOR RUNNER REGISTRATION.

---

## 1. Hardware requirements

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 4 logical processors | **8+** |
| RAM | 16 GB | **32 GB** |
| System disk | 128 GB free after OS | **256 GB+ SSD** free |
| Artifact / Docker disk | Shared OK | Separate volume ≥ 200 GB for Docker data + artifacts |
| Network | Outbound HTTPS | Same region as AWS ECR preferred (`ap-south-1`) |

---

## 2. Windows edition

| Edition | Status |
|---|---|
| **Windows Server 2022** (Standard/Datacenter) | Preferred |
| **Windows 11 Pro** | Accepted for lab / single-operator host |
| Windows Home | **Not accepted** (Hyper-V / Docker Desktop limitations) |
| Windows Server Core only | Not preferred for Docker Desktop UI troubleshooting |

---

## 3. BIOS / firmware virtualization

Enable in firmware before OS feature install:

- Intel VT-x / AMD-V  
- SLAT / EPT  
- Virtualization Technology  
- Disable conflicting “Security Device Support” quirks only if vendor docs require for Hyper-V  

**Verify after boot (elevated):**

```powershell
systeminfo | Select-String -Pattern 'Hyper-V|Virtualization'
```

Expect a hypervisor or “virtualization enabled in firmware”.

---

## 4. Administrator account requirements

| Requirement | Detail |
|---|---|
| Elevation | All feature installs and Docker Desktop setup require **Run as Administrator** |
| Local admin | Operator account in Administrators group |
| Service account (later) | Dedicated low-privilege account for GitHub Actions runner service (see runner guide) |
| UAC | Do not disable UAC permanently; elevate per session |

---

## 5. Required Windows Features

Install/enable (elevated):

| Feature | Purpose |
|---|---|
| **Hyper-V** (where available) | Virtualization stack |
| **Virtual Machine Platform** | WSL2 / Docker |
| **Windows Hypervisor Platform** | Docker Desktop / WHPX |
| **Windows Subsystem for Linux** | WSL2 |
| Containers (optional) | Not required if Docker Desktop uses WSL2 backend |

Example (elevated PowerShell):

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All -NoRestart
Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All -NoRestart
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -All -NoRestart
# Reboot when prompted
```

On Windows Server, use `Install-WindowsFeature` equivalents for Hyper-V as applicable.

---

## 6. Disk layout recommendations

```text
C:\                         # OS + tools
C:\iic-build\               # BUILD_ROOT
  repos\                    # Clean clones of release tags
  artifacts\                # RC/GA outputs
  logs\                     # Build logs
  runners\                  # GitHub Actions runner
  tools\                    # syft, trivy, helpers
D:\docker-data\             # Optional: relocate Docker disk image here
```

Keep Docker virtual disk off a nearly-full system volume. Target **&lt;70%** disk use during releases.

---

## 7. Network requirements

Outbound HTTPS (443) to at least:

| Destination | Why |
|---|---|
| `github.com`, `api.github.com`, `codeload.github.com` | Git + Actions |
| `objects.githubusercontent.com` / release CDNs | Runner + tool downloads |
| `api.nuget.org` | .NET restore |
| `registry.npmjs.org` | Node restore |
| `*.amazonaws.com` / ECR endpoint `*.dkr.ecr.ap-south-1.amazonaws.com` | AWS CLI + later ECR |
| Microsoft / Docker / VS download endpoints | Toolchain installers |

Inbound public access is **not** required for the build host.

---

## 8. Firewall requirements

| Direction | Rule |
|---|---|
| Outbound | Allow HTTPS 443 to internet / AWS / GitHub |
| Inbound | Deny public; allow RDP/WinRM only from admin jump hosts |
| Docker | Allow Docker Desktop / WSL vNIC per vendor defaults |

Corporate SSL inspection may break NuGet/npm/Docker pulls — whitelist build host or provide offline packages ([Build-Host-Offline-Package.md](Build-Host-Offline-Package.md)).

---

## 9. GitHub connectivity

Verify:

```powershell
git ls-remote https://github.com/ravisainiiitr/iic-booking-backend.git HEAD
gh auth status   # after gh install / login (optional for bootstrap)
```

Authentication for private clones: HTTPS PAT or SSH deploy key on the build host (not production secrets).

---

## 10. AWS connectivity

Verify (read-only OK):

```powershell
aws sts get-caller-identity
# Later (Batch 3): ECR login — not in this phase
```

No IAM/ECR changes during bootstrap preparation — see [AWS-ECR-Preparation.md](AWS-ECR-Preparation.md).

---

## 11. Exact installation order

Run each step as **Administrator** unless noted. Reboot when the installer or Windows Features require it, then resume.

### 1. Windows Updates

| Field | Value |
|---|---|
| Source | Settings → Windows Update (or WSUS) |
| Silent | N/A (use org patch process) |
| Version | Latest cumulative for the edition |
| Verify | Settings shows no critical pending restarts for build tools |
| Expected | Host patched; reboot complete |

### 2. PowerShell 7

| Field | Value |
|---|---|
| Source | https://aka.ms/powershell-release?tag=stable / winget `Microsoft.PowerShell` |
| Silent | `winget install --id Microsoft.PowerShell -e --accept-package-agreements --accept-source-agreements` |
| Version | **7.4+** |
| Verify | `pwsh -NoLogo -Command "$PSVersionTable.PSVersion"` |
| Expected | Major ≥ 7 |

### 3. Git

| Field | Value |
|---|---|
| Source | https://git-scm.com/download/win / winget `Git.Git` |
| Silent | `winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements` |
| Version | **≥ 2.43** |
| Verify | `git --version` |
| Expected | `git version 2.x...` |

### 4. Visual Studio Build Tools 2022

| Field | Value |
|---|---|
| Source | https://aka.ms/vs/17/release/vs_BuildTools.exe |
| Silent | `vs_BuildTools.exe --quiet --wait --norestart --add Microsoft.VisualStudio.Workload.ManagedDesktopBuildTools` |
| Version | **VS 2022** with .NET desktop build tools |
| Verify | `"${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -products * -requires Microsoft.Component.MSBuild -property installationPath` |
| Expected | Non-empty installation path |

### 5. .NET SDK 8

| Field | Value |
|---|---|
| Source | https://dotnet.microsoft.com/download/dotnet/8.0 / winget `Microsoft.DotNet.SDK.8` |
| Silent | `winget install --id Microsoft.DotNet.SDK.8 -e --accept-package-agreements --accept-source-agreements` |
| Version | **8.x** (latest patch) |
| Verify | `dotnet --list-sdks` |
| Expected | A line starting with `8.` |

### 6. Node.js 20 LTS

| Field | Value |
|---|---|
| Source | https://nodejs.org/en/download (20 LTS) / winget `OpenJS.NodeJS.LTS` (confirm major 20) |
| Silent | Prefer MSI quiet: `msiexec /i node-v20.x.x-x64.msi /qn` |
| Version | **20.x LTS** |
| Verify | `node -v` ; `npm -v` |
| Expected | `v20.…` |

### 7. WSL2

| Field | Value |
|---|---|
| Source | Windows Feature + `wsl --update` |
| Silent | `wsl --install --no-distribution` then reboot; or enable features in §5 then `wsl --set-default-version 2` |
| Version | WSL **2** |
| Verify | `wsl --status` |
| Expected | Default Version: 2 |

### 8. Ubuntu LTS distribution

| Field | Value |
|---|---|
| Source | `wsl --install -d Ubuntu-22.04` (or Ubuntu-24.04) |
| Silent | `wsl --install -d Ubuntu-22.04 --no-launch` (complete user setup once) |
| Version | Ubuntu **22.04 or 24.04 LTS** |
| Verify | `wsl -l -v` |
| Expected | Ubuntu listed; VERSION **2** |

### 9. Docker Desktop

| Field | Value |
|---|---|
| Source | https://www.docker.com/products/docker-desktop/ |
| Silent | `Docker Desktop Installer.exe install --quiet --accept-license` (see Docker docs for current flags) |
| Version | Docker Desktop **4.x** with Engine ≥ 27 |
| Verify | `docker version` ; `docker compose version` |
| Expected | Server Version present; Compose v2 plugin |

Enable **Linux containers** / WSL2 backend. Set `DOCKER_BUILDKIT=1` (machine env).

### 10. AWS CLI

| Field | Value |
|---|---|
| Source | https://aws.amazon.com/cli/ / winget `Amazon.AWSCLI` |
| Silent | `winget install --id Amazon.AWSCLI -e --accept-package-agreements --accept-source-agreements` |
| Version | **v2** |
| Verify | `aws --version` |
| Expected | `aws-cli/2.…` |

### 11. GitHub CLI

| Field | Value |
|---|---|
| Source | https://cli.github.com/ / winget `GitHub.cli` |
| Silent | `winget install --id GitHub.cli -e --accept-package-agreements --accept-source-agreements` |
| Version | Latest stable |
| Verify | `gh --version` |
| Expected | `gh version …` |

### 12. Syft

| Field | Value |
|---|---|
| Source | https://github.com/anchore/syft/releases |
| Silent | Download Windows amd64 zip; expand to `C:\iic-build\tools\syft\` ; add to PATH |
| Version | Latest stable |
| Verify | `syft version` |
| Expected | Version string printed |

### 13. Trivy

| Field | Value |
|---|---|
| Source | https://github.com/aquasecurity/trivy/releases |
| Silent | Download Windows amd64 zip; expand to `C:\iic-build\tools\trivy\` ; add to PATH |
| Version | Latest stable |
| Verify | `trivy version` |
| Expected | Version string printed |

---

## 12. Directory bootstrap

```powershell
# From repo checkout (elevated recommended)
cd <repo>\scripts\build-host
.\Initialize-BuildDirectories.ps1 -BuildRoot C:\iic-build
```

---

## 13. Final verification

```powershell
# Elevated PowerShell 7 recommended
cd <repo>\scripts\build-host
.\Verify-BuildHostReady.ps1
```

Expect overall **PASS** before runner registration.

---

## See also

- Offline media: [Build-Host-Offline-Package.md](Build-Host-Offline-Package.md)  
- Runner (no register yet): [GitHub-Runner-Installation.md](GitHub-Runner-Installation.md)  
- AWS prep (no apply): [AWS-ECR-Preparation.md](AWS-ECR-Preparation.md)  
- Gates: [Build-Host-Commissioning-Gate.md](Build-Host-Commissioning-Gate.md)
