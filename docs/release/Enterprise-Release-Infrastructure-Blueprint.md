# Enterprise Release Infrastructure Blueprint

**Document type:** Permanent release engineering guide  
**Platform:** Institute Instrumentation Centre — Equipment Booking & Remote Analysis Platform  
**Status:** Accepted architecture (planning) — build host not yet provisioned  
**Related:** [Release-Infrastructure-Architecture.md](Release-Infrastructure-Architecture.md) · [Build-Host-Provisioning.md](Build-Host-Provisioning.md) · [Platform-Build-Pipeline.md](Platform-Build-Pipeline.md) · [Build-Verification-Checklist.md](Build-Verification-Checklist.md)

---

## Part 1 — Infrastructure Architecture

Canonical topology and component roles are defined in [Release-Infrastructure-Architecture.md](Release-Infrastructure-Architecture.md).

**Design principles:**

1. **Tags are immutable release truth** — never deploy from branch HEAD.  
2. **Build ≠ Runtime** — dedicated Windows build host; production EC2 is pull-only.  
3. **Two artifact planes** — containers in **AWS ECR**; Windows installers in **Deployment Center**.  
4. **One Platform Manifest** binds four git repos, digests, installers, schema, and order.  
5. **Least privilege** — IAM roles, no long-lived registry passwords on servers.

```text
Developer Workstations → GitHub Repos → GitHub Actions → Dedicated Windows Build Host
        → AWS ECR → Deployment Center → Production EC2
        → DSA / Equipment Wizard / RAA (field)
```

---

## Part 2 — GitHub Actions Architecture

All release builds run on labels such as `self-hosted`, `windows`, `iic-build`.  
CI (lint/test) may remain on `ubuntu-latest`; **release artifact jobs** must use the dedicated host.

### 2.1 Backend — `release-backend.yml`

| Aspect | Definition |
|---|---|
| **Trigger** | `push` tags matching `v*.*.*` / `v*.*.*-rc*`; `workflow_dispatch` with tag input |
| **Branch policy** | No release build from `master`/`main` push alone |
| **Tag policy** | Annotated tags only; workflow verifies `git cat-file -t` is `tag` and peels SHA |
| **Inputs** | `tag` (dispatch), optional `push_to_ecr` (default false until Batch 3+) |
| **Outputs** | Image IDs, digests (after push), build log URL |
| **Artifacts** | `Docker-Image-Manifest` fragment; optional `docker save` tarballs (short retention) |
| **Failure handling** | Fail job; no partial ECR latest retag; notify release channel |
| **Approval gates** | Environment `release-ecr` required for push; `production-deploy` never from this workflow |

### 2.2 Frontend — `release-frontend.yml`

| Aspect | Definition |
|---|---|
| **Trigger** | Same tag patterns as Backend (`v*.*.*`) |
| **Branch policy** | Tag-only release |
| **Tag policy** | Must match intended platform portal minor (documented in Platform Manifest) |
| **Inputs** | `tag`, `push_to_ecr` |
| **Outputs** | Frontend image digest; bundle size summary |
| **Artifacts** | Manifest fragment |
| **Failure handling** | Stop; do not promote Backend alone to “platform complete” |
| **Approval gates** | `release-ecr` for push |

### 2.3 Department Sync Agent — `release-dsa.yml`

| Aspect | Definition |
|---|---|
| **Trigger** | Tags `v*.*.*` / `v*.*.*-rc*` on DSA repo |
| **Branch policy** | Release from tag checkout of `recovery/*` or `release/*` history only via tag |
| **Tag policy** | Agent semver independent of portal (see Part 3) |
| **Inputs** | `tag`, `publish_dc` (default false) |
| **Outputs** | DSA Setup EXE path, Wizard publish path, SHA256 |
| **Artifacts** | Installers + `ArtifactChecksums` fragment (Actions artifact store ≤ 90 days) |
| **Failure handling** | No DC upload on failure |
| **Approval gates** | `deployment-center-upload` for portal publish |

### 2.4 Remote Analysis Agent — `release-raa.yml`

| Aspect | Definition |
|---|---|
| **Trigger** | Tags `v*.*.*` / `v*.*.*-rc*` |
| **Branch policy** | Tag-only (e.g. from `release/*` history) |
| **Tag policy** | Prefer git tag as DC version; record legacy `VERSION` file if divergent |
| **Inputs** | `tag`, `publish_dc` |
| **Outputs** | `RemoteAnalysisAgentSetup.exe`, SHA256, dependency note |
| **Artifacts** | Installer + checksum |
| **Failure handling** | Block platform release aggregator |
| **Approval gates** | `deployment-center-upload` |

### 2.5 Platform Release — `release-platform.yml`

| Aspect | Definition |
|---|---|
| **Trigger** | `workflow_dispatch` with platform version + four tag inputs; optional `repository_dispatch` when all child workflows succeed |
| **Branch policy** | N/A (aggregator) |
| **Tag policy** | Validates Backend/Frontend/DSA/RAA tags resolve and match freeze/manifest SHAs |
| **Inputs** | `platform_version`, `backend_tag`, `frontend_tag`, `dsa_tag`, `raa_tag`, `push_ecr`, `upload_dc` |
| **Outputs** | `Platform-*-Artifact-Manifest.md`, unified checksum file, compatibility matrix |
| **Artifacts** | Full manifest package |
| **Failure handling** | Atomic: any child fail → Platform status **Failed**; no prod notification of “ready” |
| **Approval gates** | `platform-release` (humans); separate `production-deploy` for later Batch 5+ |

**Cross-cutting:** concurrency group per platform version; cancel-in-progress false for releases; retain logs ≥ 365 days for GA.

---

## Part 3 — Version Management

### 3.1 Product version lines

| Component | Version line | Example RC | Example GA |
|---|---|---|---|
| **Platform** | Portal-aligned | `2.5.0-rc1` | `2.5.0` |
| **Backend** | = Platform | `v2.5.0-rc1` | `v2.5.0` |
| **Frontend** | = Platform | `v2.5.0-rc1` | `v2.5.0` |
| **DSA** | Independent major | `v1.0.0-rc1` | `v1.0.0` |
| **RAA** | Independent major | `v1.0.0-rc1` | `v1.0.0` |
| **Equipment Wizard** | Track DSA line unless split | `1.0.0-rc1` | `1.0.0` |

Agents may stay on `1.x` while portal moves to `2.x` / `3.x` as long as the **compatibility matrix** allows it.

### 3.2 Channel definitions

| Channel | Git tag form | Meaning | Prod default |
|---|---|---|---|
| **Alpha** | `vX.Y.Z-alpha.N` | Internal spike; may break | Never |
| **Beta** | `vX.Y.Z-beta.N` | Limited pilot | Never without waiver |
| **RC** | `vX.Y.Z-rcN` | Release candidate; freeze tip | Staging / controlled prod cutover |
| **GA** | `vX.Y.Z` | Generally available | Yes |
| **Patch / Hotfix** | `vX.Y.(Z+1)` or `vX.Y.Z+hotfix.N` → prefer bump patch `vX.Y.Z+1` | Minimal fix from GA baseline | Yes after RC optional |
| **Nightly** | `nightly-YYYYMMDD` (movable) | CI signal only | Never |

**Rules:**

- Hotfixes branch from GA tag → PR → new patch tag → full artifact rebuild.  
- Do not move or delete published GA/RC tags.  
- Platform version equals Backend/Frontend tag **without** the leading `v` in manifests (`2.5.0-rc1`).

---

## Part 4 — AWS Architecture (ECR)

### 4.1 Repository layout (`ap-south-1`)

| ECR repository | Contents |
|---|---|
| `iic/booking-django` | Portal Django/Gunicorn |
| `iic/booking-celeryworker` | Celery worker |
| `iic/booking-celerybeat` | Celery beat |
| `iic/booking-flower` | Flower (optional profile) |
| `iic/booking-frontend` | Frontend static nginx/image |
| `iic/reverse-tunnel-gateway` | Gateway (when rebuilt under this program) |

Tag images as `2.5.0-rc1`, `2.5.0`, and always record **digest** `sha256:…`.

### 4.2 Lifecycle & retention

| Policy | Setting |
|---|---|
| Keep last **N** GA digests | N ≥ 10 |
| Keep all tags matching `*-rc*` | 90 days then expire untagged intermediates |
| Untagged images | Expire after 14 days |
| Nightly tags | Expire after 7 days |

### 4.3 Scanning, encryption, IAM

| Control | Recommendation |
|---|---|
| **Scanning** | Enable ECR enhanced/basic scan on push; gate GA on Critical=0 (policy) |
| **Encryption** | AES256 (AWS-managed) minimum; CMK optional for GA |
| **Build push role** | `iic-ecr-push` on build host / OIDC — `ecr:PutImage`, `InitiateLayerUpload`, etc. on `iic/*` only |
| **Prod pull role** | EC2 instance profile `iic-ecr-pull` — `ecr:GetAuthorizationToken`, `BatchGetImage`, `GetDownloadUrlForLayer` |
| **No long-lived passwords** | Prefer instance profile + `aws ecr get-login-password` in deploy scripts; rotate any static keys to zero |

---

## Part 5 — Deployment Center Architecture

Deployment Center is the **system of record for field installers**, not for portal containers.

### 5.1 Stored objects

| Object | Fields |
|---|---|
| DSA installer | version, file blob/S3 key, size, SHA256, release notes URL |
| Equipment Wizard | same |
| RAA installer | same |
| Checksums | SHA256 (and optional Authenticode thumbprint) |
| Release notes | markdown/HTML per version |
| Compatibility matrix | rows: artifact version ↔ portal min/max |
| `min_platform_version` | e.g. `2.5.0-rc1` |
| `max_platform_version` | nullable = no upper bound |
| **Release status** | `RC` · `GA` · `Deprecated` · `Withdrawn` |

### 5.2 Status semantics

| Status | Download | New enrollments | Notes |
|---|---|---|---|
| **RC** | Allowed for commissioning | Allowed with ops approval | Default for `*-rc*` |
| **GA** | Default | Default | Prod standard |
| **Deprecated** | Allowed | Discouraged / warned | Superseded but rollback-capable |
| **Withdrawn** | Blocked | Blocked | Security or broken release |

### 5.3 Publishing flow

Build host produces binaries → checksum verify → admin/API upload → matrix row → status=RC → after acceptance promote to GA (metadata only; do not rebuild).

---

## Part 6 — Release Manifest Standard

Every Platform release (RC or GA) MUST produce a manifest conforming to:

```yaml
platform_version: "2.5.0-rc1"
released_at: "ISO-8601"
status: "RC"   # RC | GA | Deprecated | Withdrawn

repositories:
  backend:
    tag: "v2.5.0-rc1"
    sha: "c512199…"
  frontend:
    tag: "v2.5.0-rc1"
    sha: "e548c79…"
  dsa:
    tag: "v1.0.0-rc1"
    sha: "495e27b…"
  raa:
    tag: "v1.0.0-rc1"
    sha: "170d689…"

docker_images:
  - name: iic/booking-django
    tag: "2.5.0-rc1"
    digest: "sha256:…"
  # celeryworker, celerybeat, flower, frontend, gateway…

installers:
  - name: dsa
    version: "1.0.0-rc1"
    sha256: "…"
  - name: equipment_wizard
    version: "1.0.0-rc1"
    sha256: "…"
  - name: raa
    version: "1.0.0-rc1"
    sha256: "…"
    legacy_version_file: "1.0.0-RT-RC1"  # optional correlation

database:
  engine: postgres
  schema_apps:
    remote_analysis: "0020"
    sync: "0018"
    # …
  migration_level: "heads@tag"   # verified showmigrations at build or deploy time

compatibility_matrix:
  - { a: backend, b: frontend, result: PASS }
  # …

deployment_order:
  - backend
  - frontend
  - deployment_center_metadata
  - dsa
  - equipment_wizard
  - raa
  - e2e_commissioning

rollback_order:
  - raa
  - equipment_wizard
  - dsa
  - frontend
  - backend
  - database_restore_if_needed
```

Human-readable Markdown exports (`Platform-*-Artifact-Manifest.md`) MUST include the same fields.

---

## Part 7 — Rollback Strategy

| Plane | Rollback method | Data caution |
|---|---|---|
| **Portal (Backend)** | Retarget compose to previous ECR digests; `up -d`; restart workers | If migrations applied, prefer forward-fix; else restore RDS snapshot from pre-migrate backup |
| **Frontend** | Previous frontend digest | Stateless; low risk |
| **DSA** | DC prior GA/RC installer; uninstall/reinstall or side-by-side per runbook | Local DB/cache on PC — backup if applicable |
| **RAA** | Prior installer; re-enroll if identity broken | Active sessions: drain first |
| **Deployment Center** | Prior metadata rows; status=Withdrawn on bad release | Keep blobs for deprecated versions |
| **Docker Images** | Digests immutable — rollback = point to old digest (never rewrite tag history silently) | Retag `2.5.0` only with new digest after approval |
| **Database** | Pre-deploy RDS snapshot / `backup.sh` dump; restore runbook | RPO-bound; test restore quarterly |

**Rule:** Application rollback without DB restore is preferred when migrations are additive and backward-compatible; otherwise treat as **data plane rollback** with explicit approval.

---

## Part 8 — Disaster Recovery

| Loss scenario | Recovery | RTO (target) | RPO (target) |
|---|---|---|---|
| **Repository loss** | GitHub org recovery / mirrors; tags + release manifests as rebuild truth | 8–24 h | 0 for pushed git |
| **Build host loss** | Rebuild from [Build-Host-Provisioning.md](Build-Host-Provisioning.md); re-register runner; rebuild from tags | 24–48 h | Artifacts regenerable from git |
| **Registry (ECR) loss** | Rebuild & push from tags; keep optional offline `docker save` for last GA | 24 h | Last published digests |
| **Production EC2 loss** | New instance + compose + pull digests + secrets from secure store; Apache/TLS | 4–12 h | App config/secrets backups |
| **RDS loss** | PITR / snapshot restore | 1–4 h | ≤ 5–15 min (PITR) |
| **Deployment Center loss** | DB restore + re-upload installers from artifact archive | 8–24 h | Installer archive daily |

**Recovery priority:** RDS → Prod EC2 (portal API) → Frontend → ECR/images → Deployment Center → field agents.

---

## Part 9 — Security

| Area | Control |
|---|---|
| **Secrets** | Prod secrets only on EC2/secret store; build host gets git + ECR push only |
| **Certificates** | TLS on Apache (`*.iitr.ac.in`); renew calendar; private keys never in git/images |
| **Artifact integrity** | SHA256 for all installers; digest pin for all images |
| **Installer signing** | Phase 2+: Authenticode with org code-signing cert |
| **Image signing** | Phase 3: Notation/Cosign + admission policy |
| **Registry auth** | IAM roles; no Docker Hub password on prod |
| **GitHub Secrets** | Minimal: runner reg, optional OIDC role ARN; no RDS password |
| **AWS IAM** | Separate push vs pull roles; deny `*:*` on build account keys |
| **Least privilege** | Release environments with required reviewers |

---

## Part 10 — Build Host Specification (final)

| Item | Final choice |
|---|---|
| **Hardware** | 8 vCPU, 32 GB RAM, 256 GB SSD (AWS `ap-south-1` non-prod subnet) |
| **OS** | Windows Server 2022 |
| **Software** | Git, VS 2022 Build Tools, .NET 8 SDK, Node 20, Docker Desktop, AWS CLI v2, GH Runner, PS 7 |
| **Disk layout** | `C:\iic-build\{runners,repos,artifacts,logs,tools}` — artifacts volume backup-enabled |
| **Backup** | Daily AMI or volume snapshot; retain 14 days; exclude huge Docker cache from file backup if AMI covers OS |
| **Monitoring** | Disk %, Docker root size, runner online heartbeats, fail alerts on workflow |
| **Windows Updates** | Monthly maintenance window; reboot; re-run smoke checklist |
| **Docker cache** | Keep BuildKit cache; prune weekly to &lt;70% disk; never prune mid-release |

---

## Part 11 — Release Lifecycle

```text
Developer → Feature Branch → PR (CI) → Merge to mainline
        → Release Branch (optional hotfix/RC stabilization)
        → RC Tag (annotated, immutable)
        → GitHub Actions (platform / per-repo release)
        → Images (ECR) + Installers (artifacts)
        → Deployment Center (metadata RC)
        → Staging / controlled commissioning
        → Production (digest pull + migrate + smoke)
        → GA Tag / DC status=GA
        → Hotfix (patch tag → same pipeline)
```

**Deployment order (runtime):** Backend → Frontend → DC metadata → one DSA → Wizard/PC → one RAA → E2E.  
**Rollback order:** reverse field agents first, then frontend, then backend (± DB).

---

## Part 12 — Future Automation Roadmap

### Phase 1 — Manual Release (current → immediate next)

- Tags pushed manually (Batch 1 done).  
- Build host provisioned; Batch 2 run with checklist.  
- ECR push and DC upload with human approval.  
- Prod deploy interactive (Phase B stages).

### Phase 2 — Semi-automated Release

- Per-repo + Platform GHA workflows on tags.  
- Auto-generate manifests & checksums.  
- ECR push on approval environment.  
- DC upload API from workflow with approval.  
- Slack/email release digest.

### Phase 3 — Fully Automated Enterprise CI/CD

- OIDC to AWS; cosign/notation; SBOM.  
- Compatibility matrix tests in CI.  
- Staged deploy workflow with automatic smoke and auto-rollback on health fail.  
- Fleet upgrade rings for DSA/RAA via Deployment Center.  
- Policy-as-code for tag immutability and required reviewers.

---

## Operating rules (standing)

1. Do not build from branches or dirty trees.  
2. Do not use production EC2 as a build host.  
3. Do not deploy without a completed Platform Manifest.  
4. Do not promote DC status to GA without commissioning evidence.  
5. Do not rewrite published tags.

---

## Document control

| Version | Date | Notes |
|---|---|---|
| 1.0 | 2026-08-04 | Initial blueprint accepted for pre-provisioning review |

**STOP:** Do not provision the Build Host until explicitly authorized.
