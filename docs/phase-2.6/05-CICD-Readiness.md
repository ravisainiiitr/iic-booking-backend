# CI/CD Readiness — Phase 2.6

Recommendations only — do not add workflows in this phase.

---

## Portal Backend

| Workflow | Present? | Recommendation |
|----------|----------|----------------|
| Build / lint / test | Partial (existing suite; confirm on PR) | `ci.yml`: checkout → uv sync → pytest subset → migrate smoke |
| Deploy | Yes — push to `master` → self-hosted `deploy.sh` | Keep; **add** tag/release channel for RC (do not auto-prod on every commit to release branch) |
| Publish | Installer publish is manual manage.py | Optional staging job to attach artifacts |
| Version stamp | Weak | Inject `GIT_SHA` / version into health endpoint |

---

## Portal Frontend

| Workflow | Present? | Recommendation |
|----------|----------|----------------|
| Build | deploy.yml exists | PR: `npm ci && npm run build` |
| Test | Sparse | Add smoke/e2e later |
| Publish | Deploy pipeline | Record build SHA in `index.html` meta or `version.json` |
| Release | — | Tag `frontend-v*` → build image → push registry |

---

## DSA

| Workflow | Present? | Recommendation |
|----------|----------|----------------|
| Integration tests | `integration-tests.yml` | Keep green on `main`/`develop` |
| Build | — | `dotnet build` matrix |
| Publish installer | — | **Add** `release.yml` on tag: run `Publish-DsaInstaller.ps1`, upload EXE+SHA256 as GH Release assets |
| Version stamp | `-Version` param | Require tag name == version |

---

## Equipment Wizard

| Workflow | Present? | Recommendation |
|----------|----------|----------------|
| All | None dedicated | Job in DSA `release.yml` matrix or separate artifact name |

---

## RAA

| Workflow | Present? | Recommendation |
|----------|----------|----------------|
| All | **None** | After first commits: `ci.yml` (build/test) + `release.yml` (publish + SHA256 assets) |

---

## Minimum CI bar before RC1 builds

1. PR checks must fail on dirty/forbidden paths (`artifacts/**/*.exe`).  
2. Release workflows run **only on tags**.  
3. Artifacts retained ≥ 90 days with SHA256 in job summary.  
4. No deploy-to-production from dirty or non-tag refs.
