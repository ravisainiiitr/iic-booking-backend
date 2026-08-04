# Known Issues — Platform 2.5.0-rc1

| ID | Severity | Issue | Mitigation / waiver |
|----|----------|-------|---------------------|
| REL-01 | **Blocker** | Phase 2.5 code not on `origin/master`; only local WT | Must create release commits before RC build |
| REL-02 | **Blocker** | RAA repository has no git commits | Initial history + publish script before agent RC |
| REL-03 | High | DSA detached HEAD + huge dirty tree; installer artifacts untracked | Clean branch; ignore `artifacts/` |
| REL-04 | High | Lab models vs untracked SAT migrations 0002/0003 drift risk | Ship together |
| H-06 | High | DSA restart/upgrade command completeness | Track; waive only with SAT evidence |
| H-10 | Medium–High | Fleet API N+1 at large scale | Perf test; index/prefetch follow-up |
| H-11 | Medium | Diagnostics depth | Node-scoped OK; expand post-RC if needed |
| REL-05 | Medium | Authenticode signing may be absent on RC installers | SHA-256 mandatory; sign before GA |
| REL-06 | Medium | Missing automated tests for Deployment Center / SAT APIs | Lab SAT + add tests before GA |
| REL-07 | Low | Guacamole session recording | N/A if not implemented |
| REL-08 | Deferred | SMS/WhatsApp, temp sensors, mTLS | Phase 3+ |

Update this table as SAT finds defects; link Test Dashboard defect IDs.
