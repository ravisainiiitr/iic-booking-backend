# Known Issues Register

Use this during commissioning and go-live decisions.

| Severity | Issue | Impact | Workaround | Owner | Target Release |
|---|---|---|---|---|---|
| Critical | None recorded at phase start |  |  |  |  |
| High | Integrated load and soak evidence pending for full RC1 GO | Operational uncertainty under peak load | Run staged soak tests before final GO | Platform Ops | RC1 commissioning window |
| High | Installer signing/hash publication evidence pending | Distribution trust/compliance risk | Restrict rollout to signed/verified artifacts only | Release Engineering | RC1 commissioning window |
| Medium | Some advanced API surfaces not deeply contract-tested | Potential edge-case runtime incompatibility | Prioritize top-risk endpoint contract tests | Backend/API Team | RC1+1 |
| Medium | Role-based UAT across all personas pending completion | Authorization workflow gaps may remain | Execute role checklist before GO | Product + Ops | RC1 commissioning window |
| Low | Documentation drift risk across multi-repo operations | Operator confusion over time | Single source of truth in release package docs | Release Manager | Continuous |
| Future Enhancement | Automated cross-repo contract verification pipeline | Faster regression detection | Build contract-test CI suite | Platform Engineering | 2.6 |
| Technical Debt | Centralized env matrix not yet fully normalized | Config drift risk | Maintain audited env sheet with approvals | DevOps | RC1+1 |
