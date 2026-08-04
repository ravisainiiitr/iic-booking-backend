# Build Validation — Local-state independence

**Question:** Can release binaries be produced without relying on a developer laptop’s dirty tree?

| Component | Clean-tag build possible today? | Local-state dependencies found | Required fix before RC |
|-----------|----------------------------------|--------------------------------|-------------------------|
| Portal Backend | **No for 2.5** (code not on remote tip) | Dirty WT; staged/untracked Phase 2.5 | Commits + CI from tag |
| Portal Frontend | **No for 2.5** | Untracked pages; unstaged App/Dashboard | Commits + CI from tag |
| DSA installer | **Script exists** but source not cleanly tagged | Relies on local `artifacts/` if someone packs wrong folder; needs npm+dotnet on builder | CI runner with SDK; ignore artifacts in git |
| Wizard | Source in WT | No standardized CI publish observed | Document + CI |
| RAA | **No** | Entire tree local-only; SQLite under `data/` | Initial commit + publish script + CI |

## Docker

| Build | Independent of laptop WT? | Notes |
|-------|---------------------------|-------|
| `compose/production/django/Dockerfile` | Yes **if** build context is clean git checkout | Uses `uv.lock` |
| Frontend production Dockerfile | Yes **if** clean checkout + `VITE_API_URL` | `npm ci` |

## CI observations

| Repo | Deploy trigger | Gap |
|------|----------------|-----|
| Backend | Push to `master` → self-hosted `deploy.sh` | No tag-based RC channel documented |
| Frontend | `deploy.yml` present | Confirm artifact retention + SHA stamping |

## Recommendations

1. **Release builds only from annotated tags** on clean CI agents.  
2. Fail pipeline if `git status --porcelain` non-empty.  
3. Upload installers to portal from CI artifacts (SHA256 in job log → Manifest).  
4. Never copy from developer `artifacts/dsa-installer` into production publish without verifying commit match.
