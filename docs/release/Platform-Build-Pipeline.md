# Platform Build Pipeline — RC1 Artifact Generation

**Purpose:** Permanent, reproducible sequence to produce Platform `2.5.0-rc1` (and future RC/GA) artifacts.  
**Forbidden in this pipeline:** deploy, migrate, production server access, registry push (until Batch 3), source commits, tag mutation.

**Inputs:** Published git tags only (see Platform RC1 Publication Report).  
**Outputs:** Docker images (local), installers, SHA256 file, digests, manifests under `ARTIFACT_ROOT`.

---

## Pipeline overview

```text
Backend images (tag v2.5.0-rc1)
        ↓
Frontend image (tag v2.5.0-rc1)
        ↓
DSA installer (tag v1.0.0-rc1)
        ↓
Equipment Wizard (tag v1.0.0-rc1 / DSA tree)
        ↓
RAA installer (tag v1.0.0-rc1)
        ↓
Checksums (SHA256)
        ↓
Docker digests (inspect / push-prep)
        ↓
Release manifests
        ↓
Artifact verification (local only)
```

Stop the pipeline on first failed stage. Do not continue to checksums/manifests with partial unverified artifacts unless a written waiver splits the batch.

---

## Stage A — Workspace hygiene (every repo, every run)

```text
git fetch --tags origin
git checkout <approved-tag>
git rev-parse HEAD          # must equal freeze certificate SHA
git status --porcelain      # must be empty (no dirty tracked files)
```

If SHA mismatch or dirty tracked tree → **STOP**.

Untracked build outputs must live only under `ARTIFACT_ROOT` / `.gitignore` paths — never mix into release commits.

---

## Stage B — Backend Docker images

| Item | Value |
|---|---|
| Repo | `iic-booking-backend` |
| Tag | `v2.5.0-rc1` |
| SHA | `c512199d61aac10a1155e7667dbb083d797fc481` |
| Product baseline note | B8 `4ed8235…` is ancestor; build from **tag** checkout |
| Compose | `docker-compose.production.yml` (images) |
| Dockerfile | `compose/production/django/Dockerfile` |

**Build:**

```bash
export DOCKER_BUILDKIT=1
docker compose -f docker-compose.production.yml build django celeryworker celerybeat
docker compose -f docker-compose.production.yml --profile flower build flower
```

**Tag for release (local):**

```bash
for s in django celeryworker celerybeat flower; do
  docker tag iic_booking_production_$s iic_booking_production_$s:2.5.0-rc1
done
```

**Record:** image ID, `RepoDigests` (if any), `Created`, build duration, warnings.

---

## Stage C — Frontend Docker image

| Item | Value |
|---|---|
| Repo | `iic-booking-frontend` |
| Tag | `v2.5.0-rc1` |
| SHA | `e548c7962af84c611543b03e723ea76683e49476` |
| Dockerfile | `compose/production/Dockerfile` |
| Image | `iic_booking_production_frontend:2.5.0-rc1` |

```bash
docker compose -f docker-compose.production.yml build frontend
docker tag iic_booking_production_frontend iic_booking_production_frontend:2.5.0-rc1
```

**Record:** image ID, digest placeholder, static asset / bundle size summary from build log or `docker history`.

---

## Stage D — DSA installer

| Item | Value |
|---|---|
| Repo | `DepartmentSyncAgent` |
| Tag | `v1.0.0-rc1` |
| SHA | `495e27b56377b1168328189ad82f2bfeee2be826` |
| Script | `scripts\Publish-DsaInstaller.ps1` |
| Version | `1.0.0-rc1` (from `VERSION` / `-Version`) |

```powershell
.\scripts\Publish-DsaInstaller.ps1 -Configuration Release -Version 1.0.0-rc1 -OutDir $env:ARTIFACT_ROOT\installers\dsa
```

**Expected outputs:** `DepartmentSyncAgentSetup.exe` (name per script), optional ZIP, SHA256 sidecar if script emits one — copy into `ARTIFACT_ROOT`.

---

## Stage E — Equipment PC Configuration Wizard

| Item | Value |
|---|---|
| Repo | `DepartmentSyncAgent` (same tag as DSA) |
| Project | `Backend\src\EquipmentPcConfigurationWizard\EquipmentPcConfigurationWizard.csproj` |
| Version | `1.0.0-rc1` |

```powershell
dotnet publish Backend\src\EquipmentPcConfigurationWizard\EquipmentPcConfigurationWizard.csproj `
  -c Release -r win-x64 --self-contained true `
  -o $env:ARTIFACT_ROOT\installers\wizard `
  /p:Version=1.0.0-rc1
```

(Adjust single-file flags to match project defaults if present.)

---

## Stage F — RAA installer

| Item | Value |
|---|---|
| Repo | `RemoteAnalysisAgent` |
| Tag | `v1.0.0-rc1` |
| SHA | `170d689e7e543f73e6b328ae6566ddddc57c0b1e` |
| Script | `scripts\Publish-Installer.ps1` |
| Version arg | Prefer `1.0.0-rc1` for DC; note legacy file `1.0.0-RT-RC1` |

```powershell
.\scripts\Publish-Installer.ps1 -Configuration Release -Version 1.0.0-rc1 -OutDir $env:ARTIFACT_ROOT\installers\raa
```

---

## Stage G — Checksums

```powershell
Get-ChildItem $env:ARTIFACT_ROOT\installers -Recurse -Include *.exe,*.zip,*.msi |
  Get-FileHash -Algorithm SHA256 |
  ForEach-Object { "{0}  {1}" -f $_.Hash, $_.Path } |
  Set-Content $env:ARTIFACT_ROOT\checksums\ArtifactChecksums-SHA256.txt
```

Include manifest files once written.

---

## Stage H — Docker digests

```bash
docker image inspect iic_booking_production_django:2.5.0-rc1 --format '{{.Id}} {{.RepoDigests}}'
# repeat for celeryworker, celerybeat, flower, frontend
```

Until registry push, record **image ID** (`sha256:…` config) as the immutable local handle; after Batch 3, record **registry digest**.

---

## Stage I — Release manifests

Generate under `docs/release/` or `ARTIFACT_ROOT\manifests\`:

1. `Docker-Image-Manifest.md`  
2. `Installer-Manifest.md`  
3. `Platform-RC1-Artifact-Manifest.md`  
4. Update checksum list if manifests are hashed  

---

## Stage J — Artifact verification (local only)

| Artifact | Verification |
|---|---|
| Backend images | `docker run --rm --entrypoint …` health/import smoke **or** compose config up on **build host** ephemeral stack (no prod DB) |
| Frontend image | Container serves HTTP static root |
| DSA Setup EXE | Launch / `--help` or silent dry-run per installer design; do not enroll against prod without auth |
| Wizard | Process starts |
| RAA Setup EXE | Process starts |
| Checksums | Re-hash and diff |

Failure → **STOP**; record defect; do not enter Batch 3.

---

## Stage K — Handoff

Deliverable package for Batch 3 authorization:

- Local images tagged `*:2.5.0-rc1`  
- Installer binaries + `ArtifactChecksums-SHA256.txt`  
- Three manifests  
- Build logs under `BUILD_ROOT\logs\builds\`  

**No** `docker push`, **no** Deployment Center upload, **no** production pull.
