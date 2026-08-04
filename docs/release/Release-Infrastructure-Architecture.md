# Release Infrastructure Architecture

**Platform:** Institute Instrumentation Centre — Equipment Booking & Remote Analysis  
**Status:** Accepted planning baseline (pre–build-host provisioning)  
**Companion:** [Enterprise-Release-Infrastructure-Blueprint.md](Enterprise-Release-Infrastructure-Blueprint.md)

---

## End-to-end topology

```text
Developer Workstations
        ↓  (feature branches, PRs)
GitHub Repositories
  (Backend · Frontend · DSA · RAA)
        ↓  (tag / workflow_dispatch)
GitHub Actions (orchestration)
        ↓  (jobs scheduled on)
Dedicated Windows Build Host
  (Self-hosted Runner · Docker · .NET · Node)
        ↓  (docker push by digest)
AWS ECR (ap-south-1)
        ↓  (installers + metadata API)
Deployment Center (Portal)
        ↓  (compose pull by digest)
Production EC2 (pull-only runtime)
        ↓
Department Sync Agents  ·  Equipment PC Wizards  ·  Remote Analysis Agents
```

**Hard rule:** Production EC2 is **never** the build machine. It pulls immutable artifacts only.

---

## Component explanations

### 1. Developer Workstations

- Local development, unit tests, feature branches.
- Must **not** be the source of production images or signed installers.
- Push only via PR to protected branches; never push ad-hoc tags from dirty trees.

### 2. GitHub Repositories

| Repo | Role | Immutable release pointer |
|---|---|---|
| `iic-booking-backend` | Portal API, Celery, Deployment Center, migrations | `vX.Y.Z` / `vX.Y.Z-rcN` |
| `iic-booking-frontend` | SPA / static UI image | aligned portal tag |
| `DepartmentSyncAgent` | DSA service + DSA installer + Equipment Wizard | agent `vA.B.C` |
| `RemoteAnalysisAgent` | RAA service + RAA installer | agent `vA.B.C` |

Multi-repo sync is enforced by a **Platform Release Manifest**, not by assuming identical version numbers across agents and portal.

### 3. GitHub Actions

- Orchestrates checkout of **tags**, invokes build jobs, uploads workflow artifacts, and (later) pushes to ECR / notifies Deployment Center.
- Uses **OIDC / short-lived tokens** where possible; no long-lived AWS keys in workflows when instance roles on the runner suffice.
- Separate workflows per repo + one **Platform Release** aggregator (see Blueprint Part 2).

### 4. Dedicated Windows Build Host (Self-hosted Runner)

- Sole authorized location for:
  - Backend / Frontend Docker image builds  
  - DSA / Wizard / RAA `win-x64` installer publishes  
  - SHA256 and digest capture  
  - Manifest generation  
- Runs Docker Desktop (Linux engine), .NET SDK, Node, VS Build Tools.
- Registered as GitHub Actions self-hosted runner for the org/repos.
- **Not** co-located with production Apache/compose workloads.

### 5. AWS ECR

- Private image registry in **`ap-south-1`**.
- Stores portal runtime images tagged with semver + digest.
- Production EC2 authenticates via **IAM instance profile** (no Hub passwords).

### 6. Deployment Center

- Portal-hosted catalog of **agent/wizard installers**, checksums, compatibility matrix, and release status (RC/GA/Deprecated/Withdrawn).
- Consumes installer binaries + metadata produced on the build host (upload is a controlled Batch, not part of image build).
- Does **not** replace ECR for container images.

### 7. Production EC2

- Runs compose stack (Django, Celery, Redis client, Guacamole, reverse-tunnel gateway, frontend container, Apache TLS termination).
- **Pull-only:** `docker pull` by digest → migrate → up.
- Holds runtime secrets (`.envs/.production`); never used to compile RC artifacts.

### 8. Department Sync Agents

- Installed on department/equipment networks via DSA Setup from Deployment Center.
- Heartbeat / sync / config push against portal APIs version-gated by compatibility matrix.

### 9. Equipment PC Wizards

- One-shot / ops tooling for discovery, pairing, static IP, shares.
- Versioned and published like installers; tied to minimum/maximum platform versions.

### 10. Remote Analysis Agents

- Analysis PC agents: enrollment, reverse tunnel, heartbeat, desktop launch path.
- Installers from Deployment Center; runtime must match portal RA APIs and gateway policy.

---

## Trust boundaries

| Boundary | Control |
|---|---|
| Dev → GitHub | PR review, branch protection |
| GitHub → Build host | Runner registration token; tag-only release workflows |
| Build host → ECR | IAM role / OIDC; push only from release workflows |
| ECR → Prod EC2 | Pull-only IAM; digests pinned in release manifest |
| Build host → Deployment Center | Authenticated admin upload after image/install verification |
| Portal → Agents | Enrollment keys, mTLS/HTTPS, version compatibility |

---

## Data flow for a Platform RC

1. Freeze tips → annotated tags pushed (Batch 1 style).  
2. Platform Release workflow (or manual Batch 2) builds on Windows host.  
3. Images → ECR; installers → artifact store + Deployment Center.  
4. Manifest signed/archived.  
5. Prod pulls images; agents upgrade via DC when commissioned.

See the Blueprint for workflows, versioning, rollback, DR, and lifecycle detail.
