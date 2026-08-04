# Configuration Qualification

## Scope

Configuration domains reviewed:
- Portal backend/runtime
- Frontend runtime/proxy
- DSA runtime and local services
- RAA runtime and installer
- External dependencies: Gateway/Guacamole, Redis, Postgres, S3, SMTP, Deployment Center

## Configuration Domains

| Domain | Primary owner | Status | Risk |
|---|---|---|---|
| Environment variables (portal) | Backend | Present, broad and subsystem-heavy | Medium |
| Secrets management | Backend + Agents | Present, but release-time secret governance must be enforced | Medium |
| Gateway/Guacamole | Remote Analysis | Required for session/tunnel flows | Medium |
| Redis | Backend | Required for async/scheduler/cache workflows | Low-Medium |
| Postgres | Backend | Required and migration-qualified | Low-Medium |
| S3/object storage | Remote Analysis/workspace | Required for upload/result/archives | Medium |
| SMTP | Portal | Required for notification workflows where enabled | Low |
| Deployment Center | Backend B3 | Present; metadata and artifact references required | Medium |
| Reverse Tunnel | Backend B1/B2 + RAA R2 | Present; env-specific endpoint and auth settings critical | Medium |
| DSA | DSA D0-D4 | Present; enrollment URL/keys, sync settings, local API settings required | Medium |
| RAA | RAA R1-R4 | Present; register/heartbeat/inventory/commands paths and tunnel settings required | Medium |
| Equipment Wizard | DSA/Deployment ownership | Present via installer/deployment paths | Medium |

## Missing Configuration (Observed)

1. Final production secret values and rotation schedule are intentionally not embedded in repo docs.
2. Signed installer certificate configuration and trust-chain publication data are not yet finalized in release docs.
3. Full cross-repo environment matrix (exact key-by-key per environment) is not fully consolidated in a single canonical file.

## Duplicate Configuration Risk

- Potential overlap between portal deployment metadata and installer-script local configuration sources.
- Potential overlap between DSA/RAA local appsettings and portal-side policy templates.

Mitigation: enforce one source of truth per environment in final release package and deployment automation.

## Unused/Optional Configuration

- Some advanced diagnostics/operations toggles are likely optional in normal production runtime.
- Certain development and commissioning settings are not required for steady-state production but useful for support.

## Qualification Outcome

- Configuration model is functionally sufficient for RC1.
- Final qualification is **conditional** on:
  - release-environment secret injection validation,
  - end-to-end environment matrix freeze,
  - installer signature and artifact-hosting configuration proof.
