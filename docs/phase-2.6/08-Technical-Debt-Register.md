# Technical Debt Register — Phase 2.6

| ID | Severity | Item | Component | Exit criteria |
|----|----------|------|-----------|---------------|
| TD-01 | **Critical** | Phase 2.5 only in dirty WT; not on remote tip | Backend/Frontend | Releasable commits on release branch |
| TD-02 | **Critical** | RAA has no commit history | RAA | Initial commits + tag |
| TD-03 | **Critical** | DSA detached HEAD | DSA | Attach to `develop`/`release/*` with clean status |
| TD-04 | **Critical** | ~383 untracked installer binaries under `artifacts/` | DSA | gitignore + never commit; CI-only artifacts |
| TD-05 | **High** | Lab models vs untracked SAT migrations drift | Backend | Ship 0002/0003 with models |
| TD-06 | **High** | `tmp_commission_run.py` staged | Backend | Remove from index before commits |
| TD-07 | **High** | No RAA publish script / CI | RAA | Script + GitHub Actions |
| TD-08 | **High** | Empty RA `0015` stub vs `0017` restore complexity | Backend | Staging migration drill |
| TD-09 | **Medium** | Missing Deployment Center / SAT automated tests | Backend | Add smoke tests |
| TD-10 | **Medium** | Fleet N+1 (H-10) | Backend | Prefetch/perf pass |
| TD-11 | **Medium** | Dual docs trees (`docs/` vs `Documentation/`) | Backend | Index + eventual merge |
| TD-12 | **Medium** | Frontend GPS/vite diffs mixed with Phase 2 | Frontend | Separate or drop from RC |
| TD-13 | **Medium** | Prod deploy only on master push; weak RC channel | Backend | Tag-based RC workflow |
| TD-14 | **Low** | No top-level `/tests` layout | All | Optional post-GA |
| TD-15 | **Low** | Authenticode not mandated in DSA publish | DSA/RAA/Wizard | Sign before GA |
| TD-16 | **Low** | Wizard not separate repo | Wizard | Keep nested for RC1 |

**Critical count:** 4 — all must clear before RC1 engineering GO.
