# Build Host Provisioning Guide — Platform RC1 / Permanent Artifact Platform

**Audience:** Build & Artifact / DevOps  
**Scope:** Provisioning only — no application code changes, no deploy, no image push until Batch 3 authorization  
**Related:** [Platform-Build-Pipeline.md](Platform-Build-Pipeline.md), [Build-Verification-Checklist.md](Build-Verification-Checklist.md)

---

## 1. Recommended specification (Step 1)

| Resource | Recommendation | Rationale |
|---|---|---|
| **Role** | Dedicated **Build Host** — **not** production EC2 `ip-10-0-1-153` | Isolates builds from live traffic; avoids dirty-tree / disk contention |
| **Operating System** | **Windows Server 2022** (or Windows 11 Pro for lab) **64-bit** | DSA / Equipment Wizard / RAA publish scripts target `win-x64` self-contained EXE/WPF |
| **CPU** | **8 vCPU** (minimum 4) | Parallel Docker builds + `dotnet publish` + npm |
| **RAM** | **32 GB** (minimum 16 GB) | Docker Desktop VM + multiple .NET publishes + Node |
| **Disk** | **256 GB SSD** (minimum 128 GB free after OS) | Docker layers, build cache, installer payloads, artifact archive |
| **Network** | Outbound HTTPS to GitHub, NuGet, npm, container registries; no inbound public required | Tag fetch + dependency restore + (later) registry push |
| **Placement** | Same AWS region as prod preferred (`ap-south-1`) on a **non-production** subnet/SG | Low latency to ECR; SG separate from portal |

### Software versions (pin at provision time; verify before every RC build)

| Component | Recommended version | Notes |
|---|---|---|
| **Docker Engine / Desktop** | Docker Desktop **4.x** with Engine **≥ 27** (target match prod ≈ **29.x** when available) | Required for Backend/Frontend images |
| **Docker Compose** | **v2** plugin (`docker compose`) **≥ 2.24** (prod observed v5.x OK) | Use Compose V2 only |
| **Git** | **≥ 2.43** | `fetch --tags`, annotated tag checkout |
| **.NET SDK** | **SDK 8.x** (install latest 8.x patch; add 9.x only if projects require) | DSA / Wizard / RAA |
| **Node.js** | **20 LTS** (or project-required LTS; avoid odd majors) | DSA frontend bundle + portal frontend image build context |
| **npm** | Bundled with Node 20 | `npm ci` preferred in pipelines |
| **Python** | **3.12+** (optional on Windows host) | Only if local non-Docker tooling needed; portal images build Python inside Dockerfile |
| **PowerShell** | **5.1** (Windows built-in) **+** PowerShell **7.x** | Publish scripts are `#Requires -Version 5.1` |
| **Visual Studio Build Tools** | **VS 2022 Build Tools** with workload **“.NET desktop build tools”** + MSBuild | Required for WPF installer/wizard projects |
| **WSL** | **WSL2** enabled if using Docker Desktop Linux engine (default) | No need for a full Ubuntu build distro if Desktop provides the engine |
| **Make / zip** | Built-in `Compress-Archive` sufficient; 7-Zip optional | Installer scripts already zip payloads |

### Connectivity requirements

| Target | Requirement |
|---|---|
| `github.com` (HTTPS + SSH or HTTPS git) | Clone/fetch published RC1 tags |
| NuGet (`api.nuget.org`) | .NET restore |
| npm registry | Frontend / DSA UI restore |
| **Container registry** (see Registry Planning — recommended **ECR**) | Push/pull after Batch 3 auth |
| Production portal HTTPS | **Not required** for pure artifact build; required later for Deployment Center upload only |
| Production SSH | **Forbidden** for builds |

---

## 2. Required software (install order)

1. Windows updates + reboot  
2. Git for Windows  
3. Visual Studio 2022 Build Tools (`.NET desktop build tools`)  
4. .NET SDK 8.x  
5. Node.js 20 LTS  
6. PowerShell 7.x (optional but recommended)  
7. Enable virtualization / WSL2 components (for Docker Desktop)  
8. Docker Desktop for Windows (Linux containers mode)  
9. AWS CLI v2 (for ECR auth) **or** `gh` + ORAS if using GHCR instead  
10. Self-hosted GitHub Actions Runner (after host smoke tests pass)

---

## 3. Directory layout

```text
C:\iic-build\                          # BUILD_ROOT
  runners\                             # GitHub Actions runner(s)
  repos\
    iic-booking-backend\
    iic-booking-frontend\
    DepartmentSyncAgent\
    RemoteAnalysisAgent\
  artifacts\
    rc1\
      images\                          # optional docker save .tar (local only)
      installers\
        dsa\
        wizard\
        raa\
      checksums\
      manifests\
  logs\
    builds\
  tools\                               # helper scripts (no app source edits)
```

Clone **fresh** repositories under `repos\` — do not copy dirty trees from developer laptops or production.

---

## 4. Environment variables

| Variable | Purpose | Example |
|---|---|---|
| `BUILD_ROOT` | Root of layout above | `C:\iic-build` |
| `ARTIFACT_ROOT` | Output root | `%BUILD_ROOT%\artifacts\rc1` |
| `DOCKER_BUILDKIT` | Faster/reproducible builds | `1` |
| `COMPOSE_DOCKER_CLI_BUILD` | Compose BuildKit | `1` |
| `AWS_REGION` | ECR region | `ap-south-1` |
| `AWS_PROFILE` or instance role | Registry auth | build-host role |
| `REGISTRY` | Image prefix | `<account>.dkr.ecr.ap-south-1.amazonaws.com/iic` |
| `PLATFORM_VERSION` | Manifest stamping | `2.5.0-rc1` |

**Do not** store production Django `DATABASE_URL` / Omniport / Razorpay secrets on the build host. Image builds use Dockerfiles + lockfiles only.

---

## 5. Docker configuration

- Linux containers mode  
- Disk image size ≥ **100 GB**  
- Enable BuildKit  
- Log in to registry only when Batch 3 is authorized  
- Periodic `docker builder prune` on a schedule (not during an active RC cut)  
- Never point compose `env_file` at production secret files for image **build** (runtime secrets stay on prod)

---

## 6. Credential locations (build host only)

| Secret | Location | Used for |
|---|---|---|
| GitHub clone (HTTPS PAT or SSH key) | Windows CredMan / `~\.ssh` | `git fetch` tags |
| GitHub Actions runner token | Runner `.credentials` (runner-managed) | CI jobs |
| AWS IAM (ECR) | Instance profile **preferred** or `~\.aws\credentials` (locked ACLs) | `docker login` ECR |
| Code signing (optional future) | HSM or locked cert store | Installer Authenticode |
| Deployment Center upload token | **Not** on build host until Batch 7 | Portal upload |

Restrict ACLs: build service account only. No shared interactive admin passwords in scripts.

---

## 7. Registry authentication (post-provision smoke; push deferred)

```powershell
# ECR example (after IAM ready) — DO NOT push until Batch 3
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.ap-south-1.amazonaws.com
docker pull hello-world
docker logout <account>.dkr.ecr.ap-south-1.amazonaws.com
```

---

## 8. Verification commands (gate before Batch 2 resume)

```powershell
git --version
dotnet --list-sdks
node -v; npm -v
docker version
docker compose version
pwsh -NoLogo -Command "$PSVersionTable.PSVersion"
# VS Build Tools
& "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -products * -requires Microsoft.Component.MSBuild -property installationPath

# Network
git ls-remote --tags git@github.com:ravisainiiitr/iic-booking-backend.git v2.5.0-rc1
# or HTTPS equivalent
```

**PASS criteria:** all commands succeed; Docker can run `docker run --rm hello-world`; `dotnet` can `dotnet --info`; Node can `npm -v`.

---

## 9. Hard exclusions

- Do **not** install the runner on production EC2  
- Do **not** build from branch tips — tags only  
- Do **not** commit from the build host as part of artifact generation  
- Do **not** change release tags
