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
        ├── Backend Release → Windows Build Host (tests + image build qualification)
        └── Deploy Backend  → SSH → Production EC2
                                git checkout <release tag>
                                docker compose build / up -d
                                health checks
        ↓  (installers + metadata API — agents/wizards)
Deployment Center (Portal)
        ↓
Department Sync Agents  ·  Equipment PC Wizards  ·  Remote Analysis Agents
```

**Hard rule:** Backend production rolls out from an **immutable git tag** over SSH. AWS ECR is **not** used for Backend portal images. See [Backend-EC2-SSH-Deploy.md](Backend-EC2-SSH-Deploy.md).

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

- Orchestrates checkout of **tags**, invokes Build Host qualification jobs, uploads workflow artifacts.
- **Backend Deploy** uses SSH secrets (`EC2_HOST` / `EC2_USER` / `EC2_SSH_KEY`) — no AWS credentials for portal rollout.
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

### 5. Production EC2 (Backend)

- Holds a git checkout of `iic-booking-backend`.
- Receives releases via **Deploy Backend** (`git checkout <tag>` + `docker compose` build/up).
- Health: `GET /api/v1/analysis/health/ready/` (published on host port **8080** in `docker-compose.production.yml`).
- AWS ECR is **not** required for Backend portal images.

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
| Build host → qualification artifacts | Local digests/SBOM only (not a production registry) |
| GitHub → Prod EC2 | SSH (`Deploy Backend`); checkout immutable tag; compose build/up |
| Build host → Deployment Center | Authenticated admin upload after image/install verification |
| Portal → Agents | Enrollment keys, mTLS/HTTPS, version compatibility |

---

## Data flow for a Platform RC

1. Freeze tips → annotated tags pushed.  
2. **Backend Release** qualifies the tag on the Windows Build Host (tests + image build).  
3. **Deploy Backend** SSHs to production EC2, checks out the tag, builds and restarts compose.  
4. Agent/wizard installers → artifact store + Deployment Center (unchanged path).  
5. Agents upgrade via Deployment Center when commissioned.

See [Backend-EC2-SSH-Deploy.md](Backend-EC2-SSH-Deploy.md) and the Blueprint for versioning, rollback, and DR detail.
