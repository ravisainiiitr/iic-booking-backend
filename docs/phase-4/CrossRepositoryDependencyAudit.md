# Cross Repository Dependency Audit

## Scope

Integrated RC1 commit state:
- Portal Backend: B1-B8
- Frontend: F1-F4
- DSA: D0-D4
- RAA: R1-R4
- Equipment Wizard: packaged with DSA/RAA installer capabilities

## Dependency Matrix

| Dependency | API compatibility | Authentication | Configuration | Version compatibility | Protocol compatibility | Breaking changes | Missing assumptions | Risk |
|---|---|---|---|---|---|---|---|---|
| Portal -> Frontend | Frontend routes and API clients align with `/api/v1/analysis/*`, `/api/v1/deployment/*`, `/api/v1/lab/testing/*` | Portal user auth + RBAC expected | Frontend proxy/base URL and portal API host must be aligned | F1-F4 assumes B2-B7 endpoints exist | HTTP/JSON contracts compatible | None observed in RC1 chain | Exact enum/value contract for some lifecycle statuses still integration-dependent | Medium |
| Frontend -> Portal | Frontend uses documented backend APIs from Phase 2.9 inventory | User session/token auth expected | Environment-specific API base and CORS/proxy needed | Compatible with backend B-series SHAs | REST/JSON compatible | None observed | Final staging auth class and permission matrix must match production roles | Medium |
| Portal -> DSA | Portal sync/control-plane endpoints map to DSA control/data/security/update flows | Agent enrollment + bearer/key auth model present | Enrollment keys, portal URL, local config ack settings required | DSA D1-D3 assumes B4-B5 sync/lab APIs | HTTP/JSON + command queue semantics compatible | None observed | Retry/idempotency behavior under prolonged offline windows needs integrated soak validation | Medium |
| DSA -> Portal | DSA controllers/services align with portal sync/admin expectations | Agent and admin auth flows implemented | Requires portal endpoint URLs, enrollment secret, local service settings | D0-D4 built against current backend contracts | REST + polling/ack protocols compatible | None observed | Command timeout/backoff thresholds need production tuning | Medium |
| Portal -> RAA | RAA uses `/api/v1/analysis/register|heartbeat|inventory|commands|workspace*` paths from backend inventory | Agent auth flow present | Requires gateway base URL, reverse tunnel settings, workspace paths | R1-R3 matches B1-B2 execution/tunnel APIs | REST + heartbeat + command completion compatible | None observed | Multi-agent concurrency and tunnel failover behavior requires field qualification | Medium |
| RAA -> Portal | Portal installer and runtime client target documented analysis endpoints | Enrollment/agent auth implemented | Requires enrollment key, API base URL, tunnel config | Compatible with backend B1-B3 endpoint set | HTTP/JSON + tunnel command protocol compatible | None observed | Final token rotation cadence and certificate policies need release validation | Medium |
| Portal -> Equipment Wizard | Deployment/installer distribution surfaces present for wizard release/download tickets | Ticket/token-based download model present | Requires deployment center metadata and storage configuration | Depends on B3 deployment ownership and DSA/RAA installer packaging | Ticketed download protocol compatible | None observed | Exact installer signature policy and hash publication workflow still release-environment dependent | Medium |
| DSA -> Equipment Wizard | DSA includes equipment provisioning and wizard-aligned onboarding flows | Local/admin or pairing token patterns available | Requires pair/discovery/config settings | D1 aligns with wizard provisioning purpose | API + local workflow protocols compatible | None observed | End-to-end physical workstation commissioning sequence needs SAT execution evidence | Medium |

## Breaking Change Scan

- No deliberate API version namespace change detected (all remain under current `/api/v1/*` contracts or DSA local API namespace).
- No confirmed removed portal endpoints consumed by committed Frontend/DSA/RAA changes.
- Legacy compatibility aliases for booking analysis lifecycle still exist on backend, reducing immediate break risk.

## Cross-Repository Assumption Gaps

1. Enum and state-value strictness between frontend status rendering and backend lifecycle outputs is not fully contract-tested in this phase.
2. Long-running agent resilience (heartbeat gaps, tunnel churn, command replay) is build-validated but not fully environment-qualified.
3. Installer signature and artifact governance are documented but require release-environment execution for final qualification.
4. Role/permission matrix across administrator personas requires operational UAT walkthrough.

## Overall Assessment

- Integrated dependency graph is structurally compatible at RC1 commit level.
- Highest risks are operational/integration qualification risks rather than code-contract breakage.
- Recommended overall dependency risk: **Medium** pending full integration/SAT and release-environment runs.
