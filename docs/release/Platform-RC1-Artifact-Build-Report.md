# Platform RC1 Artifact Build Report — Batch 2

**Status:** **Artifact Failed** (stopped at Step 1)  
**Timestamp:** 2026-08-04 ~21:58 IST  
**Build host:** Windows workstation (`D:\IIC_NEW\…`)  
**Mode:** Build & Artifact Engineer — no deploy / no registry / no production server access

---

## STEP 1 — Backend Image Build — **FAILED**

### Pre-build verification (PASS)

| Check | Result |
|---|---|
| `git fetch --tags` | PASS |
| `git checkout v2.5.0-rc1` | PASS (detached HEAD) |
| `git rev-parse HEAD` | `c512199d61aac10a1155e7667dbb083d797fc481` |
| Freeze certificate match | **PASS** (docs/tag tip) |

### Build attempt

| Item | Result |
|---|---|
| `docker --version` | **FAIL** — `docker` not recognized |
| Docker Desktop path | Not installed (`C:\Program Files\Docker\…` missing) |
| WSL Docker | **FAIL** — no WSL distributions installed |
| Images built | **0** |

### Defect record

| Field | Value |
|---|---|
| **Artifact** | Backend Docker images (`django`, `celeryworker`, `celerybeat`, `flower`) |
| **Failure** | Cannot execute `docker compose build` — Docker CLI/engine absent on build host |
| **Expected** | Build images from published tag `v2.5.0-rc1` @ `c512199…` |
| **Actual** | Tag checkout OK; Docker unavailable; build not started |
| **Root cause** | Build workstation has .NET SDK + Node.js but **no Docker Engine / Docker Desktop / WSL** |
| **Severity** | **Blocker** for Batch 2 |
| **Impact** | Blocks Steps 1–2 (images), Step 5 image qualification, Steps 7/9 image manifests, Batch 3 registry |
| **Minimal fix** | Install and start **Docker Desktop** (Windows) **or** authorize a **dedicated non-production build host/CI runner** with Docker; re-run Batch 2 from Step 1 on that host using the same published tags |
| **Verification plan** | `docker version` → `docker compose version` → checkout `v2.5.0-rc1` → rebuild → record image IDs/digests |
| **Regression risk** | None (no images produced; no deploy) |
| **Not used** | Production EC2 Docker (forbidden by Batch 2 rules / no prod access) |

---

## Steps not executed (atomic stop)

| Step | Status |
|---|---|
| 2 Frontend Image Build | **NOT STARTED** |
| 3 DSA Installer Build | **NOT STARTED** (dotnet/npm available; deferred pending Docker gate / atomic stop) |
| 4 RAA Installer Build | **NOT STARTED** |
| 5 Artifact Qualification | **NOT STARTED** |
| 6–9 Manifests / checksums | **NOT GENERATED** (no artifacts) |
| 10 Readiness | **Artifact Failed** |

**Note:** DSA/RAA publish scripts and .NET/Node toolchains are present on this host and can proceed once the Docker gate is resolved *if* you authorize continuing installers after Docker is fixed, or building installers on this host in parallel with Docker builds elsewhere. Per Batch 2 stop rule after Step 1 failure, they were not started.

---

## Toolchain inventory (this host)

| Tool | Present |
|---|---|
| Git + published tags | Yes |
| `dotnet` | Yes (`C:\Program Files\dotnet\dotnet.exe`) |
| `node` / `npm` | Yes |
| `docker` | **No** |
| WSL | **No distros** |

---

## Recovery procedure (authorized next actions)

1. **Option A — Local Docker:** Install Docker Desktop → reboot/start engine → confirm `docker version` → re-authorize Batch 2 from Step 1.  
2. **Option B — Dedicated build host:** Provision a build VM/CI runner with Docker (not production traffic) → clone tags → re-authorize Batch 2.  
3. **Option C — Split build (requires explicit approval):** Build installers (DSA/Wizard/RAA) on this Windows host now; build Docker images on Option A/B when ready; merge into one Artifact Manifest before Batch 3.

Do **not** build images on the production EC2 unless separately authorized (violates pull-only long-term model and Batch 2 “no production server” rule).

---

## Artifact readiness decision

# Artifact Failed

| Metric | Count |
|---|---|
| Docker images produced | 0 |
| Installers produced | 0 |
| Manifests completed | 0 (this failure report only) |
| Checksums | 0 |

**Stopped.** Awaiting authorization for recovery Option A, B, or C before resuming Batch 2.
