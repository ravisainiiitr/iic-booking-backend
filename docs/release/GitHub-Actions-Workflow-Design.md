# GitHub Actions Workflow Design

**Document type:** Production-ready CI/CD workflow design  
**Status:** **Implemented as YAML** (Phase C) — **not executed**; runner not provisioned  
**Runner target:** Self-hosted Windows GitHub Runner on dedicated Build Host  
**Registry:** AWS ECR (`ap-south-1`)  
**Companion:** [Enterprise-Release-Infrastructure-Blueprint.md](Enterprise-Release-Infrastructure-Blueprint.md) · [Platform-Build-Pipeline.md](Platform-Build-Pipeline.md) · `scripts/build-host/README.md`

**Implemented workflow paths:**
- Backend: `.github/workflows/backend-release.yml`
- Platform: `.github/workflows/platform-release.yml`
- Frontend: `iic-booking-frontend/.github/workflows/frontend-release.yml`
- DSA: `DepartmentSyncAgent/.github/workflows/dsa-release.yml`
- RAA: `RemoteAnalysisAgent/.github/workflows/raa-release.yml`

**DRY_RUN:** All release workflows support `dry_run` (default `true` on `workflow_dispatch`) — skips ECR push and DC upload.

---

## 0. Design principles

1. **Tag-only releases** — never build release artifacts from branch `push`.  
2. **Verify before build** — `git rev-parse HEAD` must match expected peeled tag SHA.  
3. **Atomic failure** — any failed required job fails the workflow; no “partial success” promotion.  
4. **Push is gated** — ECR push and Deployment Center upload require GitHub **Environments** with reviewers.  
5. **Prod EC2 is out of scope** — these workflows do not SSH to production or run compose on prod.  
6. **Windows runner** — all release jobs use labels: `self-hosted`, `windows`, `iic-build`.

---

## 1. Global conventions

### 1.1 Runner labels

```yaml
runs-on: [self-hosted, windows, iic-build]
```

### 1.2 Concurrency

```yaml
concurrency:
  group: release-${{ github.repository }}-${{ github.ref }}
  cancel-in-progress: false   # never cancel an in-flight release
```

### 1.3 Permissions (least privilege)

| Permission | Default release workflow |
|---|---|
| `contents: read` | Always |
| `id-token: write` | When using OIDC → AWS |
| `packages: write` | Only if GHCR ever used (not primary) |
| `actions: read` | Cross-workflow artifact download (Platform) |
| `attestations: write` | Optional SBOM/attest (Phase 2+) |

### 1.4 Tag patterns

| Repo | Tag filter |
|---|---|
| Backend | `v2.*` (includes `v2.5.0`, `v2.5.0-rc1`) |
| Frontend | `v2.*` |
| DSA | `v1.*` (adjust when major bumps) |
| RAA | `v1.*` |

Use:

```yaml
on:
  push:
    tags:
      - 'v2.*'    # backend/frontend
```

### 1.5 Shared composite actions (planned)

| Action | Purpose |
|---|---|
| `iic/checkout-release-tag` | fetch tags, checkout, verify annotated tag, print SHA |
| `iic/aws-ecr-login` | OIDC or role assumption → `docker login` ECR |
| `iic/record-image-digest` | `docker inspect` → JSON fragment |
| `iic/sha256-file` | hash installers → checksum line |

---

## 2. Secrets & variables catalog

### 2.1 GitHub Environments

| Environment | Used by | Protection |
|---|---|---|
| `release-build` | All build jobs | Optional: self-hosted only |
| `release-ecr` | ECR push jobs | **Required reviewers** |
| `deployment-center` | DC metadata upload | **Required reviewers** |
| `platform-release` | Workflow 5 aggregator | **Required reviewers** |

### 2.2 Secrets / vars

| Name | Scope | Purpose |
|---|---|---|
| `AWS_ROLE_ARN_ECR_PUSH` | Org/repo | OIDC assume-role for ECR push |
| `AWS_REGION` | Variable | `ap-south-1` |
| `ECR_REGISTRY` | Variable | `<account>.dkr.ecr.ap-south-1.amazonaws.com` |
| `ECR_PREFIX` | Variable | `iic` |
| `DC_BASE_URL` | Variable | Portal URL for Deployment Center API |
| `DC_UPLOAD_TOKEN` | Environment `deployment-center` | Bearer/API token (short-lived preferred) |
| `SLACK_WEBHOOK_RELEASE` | Optional | Failure/success notify |

**Not stored in Actions:** RDS passwords, Django `SECRET_KEY`, Omniport, Razorpay, production `.envs`.

### 2.3 AWS authentication (ECR)

**Preferred:** GitHub OIDC → IAM role `iic-gha-ecr-push` with trust to repo + environment.

```text
Workflow job (id-token: write)
  → aws-actions/configure-aws-credentials (role-to-assume)
  → aws ecr get-login-password | docker login
  → docker push
  → docker logout
```

**Fallback (build host only):** Instance profile on the Windows build host with same push permissions; workflow skips OIDC and uses host role. Still **no long-lived access keys** in GitHub Secrets.

### 2.4 Deployment Center API authentication

| Method | Recommendation |
|---|---|
| Service account + token in Environment secret | Phase 1–2 |
| OAuth client credentials / short-lived JWT | Phase 3 |
| Cookie session of human | Forbidden in CI |

Upload only after checksum verify; API should accept version, SHA256, min/max platform, status=`RC`.

---

## 3. Workflow diagrams

### 3.1 Backend Release

```text
tag push v2.*
    │
    ▼
┌─────────────┐
│ checkout    │──verify annotated tag + SHA
└──────┬──────┘
       ▼
┌─────────────┐     ┌──────────────┐
│ unit/integ  │────▶│ build images │  (django, celery*, flower)
│ tests       │     └──────┬───────┘
└─────────────┘            ▼
                    ┌──────────────┐
                    │ digests+SBOM │
                    │ checksums    │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐  environment: release-ecr
                    │ push ECR     │  (approval gate)
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │ upload logs  │
                    │ & fragments  │
                    └──────────────┘
```

### 3.2 Frontend Release

```text
tag push v2.*
    → checkout + SHA verify
    → npm ci
    → npm run build (or Docker-stage build)
    → verify routes / build output gate
    → docker build frontend
    → [approval] push ECR
    → checksum / digest artifact
```

### 3.3 DSA Release (+ Equipment Wizard)

```text
tag push v1.*
    → checkout + SHA verify
    → restore (nuget + npm)
    → Publish-DsaInstaller.ps1
    → publish Equipment Wizard
    → SHA256
    → installer manifest fragment
    → [optional approval] DC upload
```

### 3.4 RAA Release

```text
tag push v1.*
    → checkout + SHA verify
    → restore + Publish-Installer.ps1
    → SHA256
    → manifest fragment
    → [optional approval] DC upload
```

### 3.5 Platform Release

```text
workflow_dispatch (manual) + environment approval
    → input: platform_version + 4 tags
    → verify each tag via git ls-remote / API
    → verify compatibility matrix rules
    → download child workflow artifacts OR query ECR + checksum store
    → generate Platform Release Manifest
    → [approval] publish DC metadata bundle
    → emit final status Artifact Ready / Failed
```

---

## 4. WORKFLOW 1 — Backend Release

**Proposed path:** `iic-booking-backend/.github/workflows/release-backend.yml`

### 4.1 Trigger

```yaml
on:
  push:
    tags:
      - 'v2.*'
  workflow_dispatch:
    inputs:
      tag:
        description: 'Existing tag to build (e.g. v2.5.0-rc1)'
        required: true
      push_ecr:
        type: boolean
        default: false
```

### 4.2 Jobs & dependencies

```text
verify_tag
    → test
    → build_images          (needs: test)
    → generate_metadata     (needs: build_images)  # digests, SBOM, checksums
    → push_ecr              (needs: generate_metadata, if: push enabled)
    → upload_artifacts      (needs: generate_metadata; always upload logs)
```

| Job | Runner | Key steps |
|---|---|---|
| `verify_tag` | windows self-hosted | `git fetch --tags`; checkout tag; assert annotated; `rev-parse` equals peel; write `sha.txt` |
| `test` | windows **or** ubuntu-latest if tests are Linux-centric | Prefer **Linux** for Django pytest if Docker-based CI already works; else `docker compose run` on build host |
| `build_images` | windows self-hosted | `DOCKER_BUILDKIT=1`; compose build `django` `celeryworker` `celerybeat` `flower`; tag `:VERSION` |
| `generate_metadata` | windows self-hosted | inspect digests/IDs; Syft/Trivy SBOM if installed (`sbom.cdx.json`); write `backend-images.json` |
| `push_ecr` | windows self-hosted | Environment `release-ecr`; OIDC login; retag to `ECR_REGISTRY/iic/booking-*`; push; record registry digests |
| `upload_artifacts` | windows self-hosted | Actions artifacts: logs, JSON, SBOM, checksum file |

**Version derivation:** strip leading `v` from tag → `2.5.0-rc1` for image tags.

### 4.3 Tests policy

- **Block ECR push** if `test` fails.  
- RC may allow `workflow_dispatch` input `skip_tests=false` only with environment approval (default deny).  
- Reuse existing pytest/compose patterns from `ci.yml` where possible; do not invent new test suites in the workflow design.

### 4.4 SBOM

| Tool | When |
|---|---|
| Syft / Trivy `image` | If binary present on build host |
| Skip with warning | If not installed — record `sbom: skipped` in manifest; Phase 2 makes required |

### 4.5 Outputs / artifacts

- `backend-images.json` — name, tag, image_id, digest  
- `sbom-*.json` (optional)  
- `build.log`  
- Retention: **90 days** for RC, **365 days** for GA tags (`vX.Y.Z` without `-rc`)

---

## 5. WORKFLOW 2 — Frontend Release

**Proposed path:** `iic-booking-frontend/.github/workflows/release-frontend.yml`

### 5.1 Trigger

```yaml
on:
  push:
    tags: ['v2.*']
  workflow_dispatch:
    inputs:
      tag: { required: true, type: string }
      push_ecr: { type: boolean, default: false }
```

### 5.2 Jobs & dependencies

```text
verify_tag → npm_build → verify_routes → docker_build → push_ecr → upload_artifacts
```

| Job | Steps |
|---|---|
| `verify_tag` | Same as Backend |
| `npm_build` | `npm ci`; `npm run build`; capture bundle size summary |
| `verify_routes` | Gate: `dist/` (or project output) exists; optional script listing critical routes/assets; fail if empty build |
| `docker_build` | `docker compose -f docker-compose.production.yml build`; tag `iic_booking_production_frontend:VERSION` and ECR name |
| `push_ecr` | Environment `release-ecr` |
| `upload_artifacts` | `frontend-image.json`, build log, optional `dist` size report (not full dist if huge) |

**Note:** Prefer single Docker build that runs `npm ci` inside Dockerfile for reproducibility; host `npm ci` remains a fast-fail gate before paying for image build.

---

## 6. WORKFLOW 3 — Department Sync Agent Release (+ Equipment Wizard)

**Proposed path:** `DepartmentSyncAgent/.github/workflows/release-dsa.yml`

### 6.1 Trigger

```yaml
on:
  push:
    tags: ['v1.*']
  workflow_dispatch:
    inputs:
      tag: { required: true }
      upload_dc: { type: boolean, default: false }
```

### 6.2 Jobs & dependencies

```text
verify_tag
  → restore_and_build          # nuget/npm restore inside publish script
  → publish_dsa_installer
  → publish_equipment_wizard   # can parallel with installer if no payload clash; else sequential
  → hash_and_manifest
  → upload_dc                  # optional, gated
  → upload_artifacts
```

| Job | Steps |
|---|---|
| `publish_dsa_installer` | `.\scripts\Publish-DsaInstaller.ps1 -Configuration Release -Version <ver>` |
| `publish_equipment_wizard` | `dotnet publish ...EquipmentPcConfigurationWizard.csproj -c Release -r win-x64 --self-contained true` |
| `hash_and_manifest` | `Get-FileHash -Algorithm SHA256`; write `Installer-Manifest` fragment + `ArtifactChecksums` lines |
| `upload_dc` | Environment `deployment-center`; HTTPS API upload EXE+SHA256+matrix fields |

**Failure handling:** If DSA installer fails, skip Wizard upload to DC even if Wizard built (keep local artifacts for debug).

---

## 7. WORKFLOW 4 — Remote Analysis Agent Release

**Proposed path:** `RemoteAnalysisAgent/.github/workflows/release-raa.yml`

### 7.1 Trigger

```yaml
on:
  push:
    tags: ['v1.*']
  workflow_dispatch:
    inputs:
      tag: { required: true }
      upload_dc: { type: boolean, default: false }
```

### 7.2 Jobs & dependencies

```text
verify_tag → publish_installer → hash_and_manifest → upload_dc? → upload_artifacts
```

| Job | Steps |
|---|---|
| `publish_installer` | `.\scripts\Publish-Installer.ps1 -Configuration Release -Version <ver>` |
| `hash_and_manifest` | SHA256 of `RemoteAnalysisAgentSetup.exe`; note `VERSION` file value if different |
| `upload_dc` | Environment `deployment-center` |

---

## 8. WORKFLOW 5 — Platform Release

**Proposed path:** Prefer hosting in **Backend** repo as `release-platform.yml` (platform system of record) **or** a small `iic-platform-release` meta-repo. Recommendation: **Backend repo** + `workflow_dispatch` only.

### 8.1 Trigger

```yaml
on:
  workflow_dispatch:
    inputs:
      platform_version: { required: true }      # 2.5.0-rc1
      backend_tag: { required: true }           # v2.5.0-rc1
      frontend_tag: { required: true }
      dsa_tag: { required: true }
      raa_tag: { required: true }
      publish_dc_metadata: { type: boolean, default: false }
```

Requires environment **`platform-release`** (approval) before jobs that mutate DC or declare “Platform Ready”.

### 8.2 Jobs & dependencies

```text
approve (environment)
  → verify_all_tags
  → verify_compatibility_matrix
  → collect_docker_digests      # from ECR describe-images or prior workflow artifacts
  → collect_installer_hashes    # from Actions artifacts or DC staging bucket
  → generate_platform_manifest
  → publish_dc_metadata         # optional gated
  → final_status
```

| Job | Behavior |
|---|---|
| `verify_all_tags` | `git ls-remote` each repo; peel `^{}`; compare to freeze certificate / input expected SHAs (optional SHA inputs) |
| `verify_compatibility_matrix` | Static rules file in docs/release or JSON: portal↔agents PASS table; fail on missing tag |
| `collect_docker_digests` | `aws ecr describe-images` for `2.5.0-rc1` tags; fail if any image missing |
| `collect_installer_hashes` | Download artifacts from DSA/RAA workflow runs matching tags **or** read from known S3/artifact store |
| `generate_platform_manifest` | Emit `Platform-<ver>-Release-Manifest.md` + `.json` per Part 6 blueprint |
| `publish_dc_metadata` | Upsert matrix + release notes pointers; status=`RC` |
| `final_status` | Job summary: **Platform Release Ready** or **Failed** |

**Manual approval:** GitHub Environment required reviewers **are** the approval gate (no separate “approve job” hack required).

---

## 9. Approval gates (summary)

| Action | Gate |
|---|---|
| Build on tag | None (or `release-build`) |
| Push images to ECR | Environment `release-ecr` |
| Upload installers to DC | Environment `deployment-center` |
| Declare platform release / DC matrix publish | Environment `platform-release` |
| Production EC2 deploy | **Out of scope** — separate future `deploy-production` with stricter gate |

---

## 10. Failure handling

| Event | Response |
|---|---|
| SHA / tag verify fail | Fail fast; no build |
| Test fail | Skip `push_ecr`; upload logs |
| Docker build fail | Fail workflow; retain BuildKit logs |
| ECR push fail | No retag of `:latest` on failure; alert |
| Installer publish fail | Fail; do not DC upload |
| DC upload fail | Artifacts remain in Actions; manual retry job |
| Platform collect missing digest | Fail — do not mark Ready |

**Notifications:** optional Slack on `failure()` and `success()` of push/DC jobs.

---

## 11. Retry policy

| Step type | Retry |
|---|---|
| `git fetch` / network flake | Actions `retry` action or re-run job (max 2) |
| `npm ci` / NuGet restore | 1 automatic retry |
| Docker build | **No** auto-retry (expensive; human re-run) |
| ECR push | 1 retry on `5xx`/throttle |
| DC upload | 2 retries with backoff |
| Entire workflow | Human **Re-run failed jobs** only; never silent loop |

Idempotency: re-push same image tag+digest is safe; DC upload must upsert by version.

---

## 12. Artifact retention

| Artifact class | RC retention | GA retention |
|---|---|---|
| Build logs | 90 days | 365 days |
| Image JSON / digests | 90 days | 365 days |
| SBOM | 90 days | 365 days |
| Installer EXE (Actions) | 30 days (canonical copy → DC/S3) | 30 days in Actions; **permanent** in DC/S3 |
| Platform manifest | 90 days | 365 days + git-tag companion release |

Large `docker save` tarballs: **avoid** in Actions (size limits); prefer ECR as store of record.

---

## 13. Job dependency matrix (Platform view)

```text
Backend release ──┐
Frontend release ─┼──▶ (artifacts in ECR / Actions)
DSA release ──────┤
RAA release ──────┘
         │
         ▼
Platform release (manual)
         │
         ├── verify tags + matrix
         ├── collect digests + hashes
         ├── manifest
         └── DC metadata (approved)
```

Child workflows do **not** auto-trigger Platform Release (avoids races). Humans run Workflow 5 when all four green.

---

## 14. Example ECR image names

| Local compose name | ECR repository:tag |
|---|---|
| `iic_booking_production_django` | `$ECR_REGISTRY/iic/booking-django:2.5.0-rc1` |
| `iic_booking_production_celeryworker` | `$ECR_REGISTRY/iic/booking-celeryworker:2.5.0-rc1` |
| `iic_booking_production_celerybeat` | `$ECR_REGISTRY/iic/booking-celerybeat:2.5.0-rc1` |
| `iic_booking_production_flower` | `$ECR_REGISTRY/iic/booking-flower:2.5.0-rc1` |
| `iic_booking_production_frontend` | `$ECR_REGISTRY/iic/booking-frontend:2.5.0-rc1` |

---

## 15. Implementation checklist (future — not this phase)

- [ ] Provision Windows build host + runner (`iic-build`)  
- [ ] Create IAM OIDC provider + `iic-gha-ecr-push`  
- [ ] Create ECR repositories  
- [ ] Create GitHub Environments + reviewers  
- [ ] Add workflow YAML files via controlled PR (separate authorization)  
- [ ] Dry-run `workflow_dispatch` with `push_ecr=false`  
- [ ] First ECR push for `v2.5.0-rc1` under Batch 3  

---

## 16. STOP

This document is **design only**.

- Do **not** provision the runner.  
- Do **not** execute workflows.  
- Do **not** push images or installers.  

Await authorization for Build Host provisioning and/or workflow YAML implementation PRs.
