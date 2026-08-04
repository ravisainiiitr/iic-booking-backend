# Repository Structure Recommendations — Phase 2.6

**Do not move files yet.** Recommendations only for eventual alignment.

## Target layout ( aspirational )

```text
/
  src/           # primary product code (or keep language idioms)
  tests/         # automated tests
  docs/          # operator + engineering docs
  scripts/       # build/publish/dev tooling
  tools/         # analyzers, codegen, one-off utilities
  artifacts/     # local/CI output only — NEVER commit (gitignore)
  build/         # optional staging for packagers
  .github/       # workflows
  README.md
  CHANGELOG.md
  .gitignore
```

## Per-repo mapping

### Portal Backend (Django)

| Target | Current | Recommendation |
|--------|---------|----------------|
| `src/` | `iic_booking/`, `config/` | **Keep Django idiom**; do not force `/src` rename for RC1 |
| `tests/` | in-app `*/tests/` | Keep; optional top-level `tests/` later |
| `docs/` | `docs/`, `Documentation/` | Consolidate pointers in README; merge folders post-RC |
| `scripts/` | `scripts/deploy/` | Keep |
| `artifacts/` | not used | Add ignored `artifacts/` for local exports |
| `.github/` | present | Keep |

### Portal Frontend (Vite)

| Target | Current | Recommendation |
|--------|---------|----------------|
| `src/` | `src/` | Already aligned |
| `tests/` | sparse | Add `tests/` or `src/**/*.test.ts` later |
| `docs/` | minimal | Add `docs/` for FE-only notes or link portal docs |
| `scripts/` | optional | Add version-stamp script |
| `artifacts/` | `dist/` ignored | Keep `dist/` ignored; CI uploads elsewhere |
| `.github/` | present | Keep |

### DSA

| Target | Current | Recommendation |
|--------|---------|----------------|
| `src/` | `Backend/src/`, `Frontend/` | Accept .NET solution layout; document as canonical |
| `tests/` | under Backend test projects | Ensure discoverable in README |
| `docs/` | `docs/`, `Documentation/` | Unify index |
| `scripts/` | `scripts/` | Keep Publish scripts |
| `artifacts/` | **top-level used but poorly ignored** | Create ignored `artifacts/`; stop committing outputs |
| Wizard | under `Backend/src/EquipmentPcConfigurationWizard` | Keep for RC1; document component path |

### RAA

| Target | Current | Recommendation |
|--------|---------|----------------|
| `src/` | `src/RemoteAnalysis.Agent/` | Good |
| `tests/` | missing / unclear | Add `tests/` or test project before GA |
| `docs/` | `Documentation/` | Add `docs/` or rename later |
| `scripts/` | missing publish | **Add** `scripts/Publish-RaAgent.ps1` |
| `artifacts/` | bin/obj local | gitignore only |
| `.github/` | **missing** | **Add** |

## Consistency rule for RC1

Do **not** restructure trees before first releasable commits. Structure cleanup is a **post-RC1** hygiene epic unless required for ignore/CI.
