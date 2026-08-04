# Build Reproducibility Review

**Scope:** Rebuild each component from a clean machine using only VCS + documented secrets/tools.  
**Evidence date:** 2026-08-04 (working-tree / docs audit — commits not yet created).

---

## Portal Backend

| Question | Finding |
|----------|---------|
| Rebuild from scratch? | **Yes (intended)** via `compose/production/django/Dockerfile` + `uv.lock` / `pyproject.toml` |
| Clean checkout required? | **Yes** — current dirty WT is not a release input |
| Docker | Present: `compose/production/django/Dockerfile`, `docker-compose.ra-production.yml`, `deploy.sh` |
| Env template | `docs/release/rc1/sample.env.production` (+ Phase 2.5 may need lab/celery extras — document) |
| Migrations | Must apply from committed migration files only |

### Missing / gaps

| Gap | Recommendation |
|-----|----------------|
| No committed Phase 2.5 code on master | Create release history before any RC build |
| `tmp_commission_run.py` in WT | Exclude from release tree |
| Celery beat entry for lab detectors | Verify in settings/docs for RC |
| Image digest pinning in compose | Prefer digests in prod compose for RC |

---

## Portal Frontend

| Question | Finding |
|----------|---------|
| Rebuild from scratch? | **Yes (intended)** via `compose/production/Dockerfile` (`npm ci` + `vite build`) |
| Build arg | `VITE_API_URL` required for correct API target |
| Clean checkout | **Required** — Lab/Deploy/SAT pages currently untracked |

### Missing / gaps

| Gap | Recommendation |
|-----|----------------|
| Pages not in git tip | Commit before RC image build |
| No recorded production bundle SHA in CI artifact metadata | Emit `BUILD_SHA` / `VITE_APP_VERSION` into bundle for traceability |

---

## Department Sync Agent

| Question | Finding |
|----------|---------|
| Rebuild from scratch? | **Partially** — `scripts/Publish-DsaInstaller.ps1` documents self-contained win-x64 publish + SHA256 |
| Prerequisites | .NET SDK 8+, Node/npm (frontend embed), PowerShell 5.1+ |
| Installer project | `DepartmentSyncAgent.Installer` present in WT |
| Artifacts | Local `artifacts/dsa-installer/**` is **build output** — not source of truth |

### Missing / gaps

| Gap | Recommendation |
|-----|----------------|
| Detached HEAD / uncommitted tree | Cannot rebuild a **named** version until commits exist |
| Top-level `artifacts/` not ignored like `Backend/artifacts/` | Ignore `artifacts/` so CI never packs DLLs into git |
| Code signing | Script writes SHA256; **Authenticode signing** not verified as mandatory in script — document cert steps |
| Version injection | Pass `-Version` explicitly; record in manifest |
| CI publish pipeline | Prefer GitHub Action that runs Publish script on tag |

---

## Equipment PC Configuration Wizard

| Question | Finding |
|----------|---------|
| Rebuild | Source under `EquipmentPcConfigurationWizard` (WT); publish path via Deployment Center `publish_equipment_wizard` |
| Gaps | Dedicated publish script/docs less prominent than DSA — document exact `dotnet publish` + portal upload |

---

## Remote Analysis Agent

| Question | Finding |
|----------|---------|
| Rebuild from scratch? | **Not yet from git** — repository has **no commits** |
| Project | `RemoteAnalysis.Agent.csproj` (net8) in WT |
| Gaps | Missing: release publish script analogous to DSA; installer packaging; CI; signing; version stamping |

### Recommendations

1. Initial commit + `.gitignore` (exclude `data/*.db`, bin/obj).  
2. Add `scripts/Publish-RaAgent.ps1` (self-contained win-x64 + SHA256).  
3. Wire `publish_ra_installer` portal command to CI artifact.

---

## Shared prerequisites checklist

| Tool | Backend | Frontend | DSA | RAA | Wizard |
|------|---------|----------|-----|-----|--------|
| Git clean checkout at tag | ✓ | ✓ | ✓ | ✓ | ✓ |
| Docker Engine | ✓ | ✓ | — | — | — |
| Node 20+ | — | ✓ | ✓ (embed) | — | — |
| .NET 8 SDK | — | — | ✓ | ✓ | ✓ |
| uv / Python 3.13 | ✓ (Docker) | — | — | — | — |
| Code-signing cert | optional portal | — | recommended | recommended | recommended |
| Secrets (env) | ✓ | build-arg | LocalApi keys | enrollment | — |

---

## Verdict

| Component | Reproducible today from remote tip? |
|-----------|-------------------------------------|
| Backend (master) | Rebuilds **old** platform — not Phase 2.5 |
| Frontend (master) | Rebuilds **old** UI — missing Lab/Deploy/SAT |
| DSA | **No** named release from clean remote tip with Phase 1/2 |
| RAA | **No** — no history |

RC1 reproducibility starts only after release commits/tags exist and CI builds from those tags.
