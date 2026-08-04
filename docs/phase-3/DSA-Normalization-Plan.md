# DSA Repository Normalization Plan

## Scope

Repository engineering only for `D:/IIC_NEW/DepartmentSyncAgent` to prepare clean architectural commits D1-D5 without losing any work.

## Repository health snapshot

- Branch: `recovery/dsa-phase-2.7`
- Total changed entries (porcelain): **1341**
- Entries with staged changes: **1338**
- Entries with unstaged changes: **971**
- Entries with both staged+unstaged state: **968**
- Full raw inventory: `docs/phase-3/_dsa-status-porcelain.txt`
- Staged list: `docs/phase-3/_dsa-staged-files.txt`
- Unstaged list: `docs/phase-3/_dsa-unstaged-files.txt`

## Capability mapping (full inventory classification)

Machine-classified complete inventory is recorded at:
- `docs/phase-3/_dsa-capability-inventory.csv`
- `docs/phase-3/_dsa-capability-summary.txt`

Counts from full inventory:
- D1 Platform Foundation: **480**
- D2 Discovery & Provisioning: **185**
- D3 Configuration Management: **567**
- D4 Monitoring & Diagnostics: **48**
- D5 Documentation & Release Assets: **61**

## Repository classification approach

- D1: startup, DI, host wiring, appsettings, core options, installer/runtime foundation.
- D2: discovery, registration/pairing/enrollment, wizard communication, network/topology assignment.
- D3: configuration push/pack/versioning/bootstrap/sync/recovery/queue/upload/result-processing data plane.
- D4: health/heartbeat/diagnostics/alerts/monitoring/log-status surfaces.
- D5: docs, guides, release assets, packaging notes, workflow collateral.

## Mixed file analysis (multi-capability touchpoints)

The following files contain cross-capability concerns and require carve decisions:

| File | Capabilities present | Separable? | Hunk carving safety | Recommended owner commit |
|---|---|---|---|---|
| `.gitignore` | D1 + D5 | Yes | Safe | D1 |
| `Backend/src/DepartmentSyncAgent.Api/Program.cs` | D1 + D2 + D3 + D4 | Partially | Medium risk | D1 (host/DI), carve D2/D3/D4 hunks later |
| `Backend/src/DepartmentSyncAgent.Api/appsettings.Development.json` | D1 + D2 + D3 + D4 | Yes | Medium | D1 baseline first |
| `Backend/src/DepartmentSyncAgent.Api/appsettings.json` | D1 + D2 + D3 + D4 | Yes | Medium | D1 baseline first |
| `Backend/src/DepartmentSyncAgent.Application/Abstractions/Persistence/ISyncCacheRepositories.cs` | D1 + D3 | Yes | Medium | D3 |
| `Backend/src/DepartmentSyncAgent.Application/Abstractions/Portal/IPortalClient.cs` | D1 + D2 + D3 | Partially | Medium risk | D2 (contract), carve D3 members |
| `Backend/src/DepartmentSyncAgent.Application/Models/Heartbeat/HeartbeatPayload.cs` | D2 + D4 | Yes | High | D4 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/BackgroundServices/UploadEngineHostedService.cs` | D1 + D3 | Partially | Medium | D3 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/DependencyInjection/InfrastructureServiceCollectionExtensions.cs` | D1 + D2 + D3 + D4 | Partially | High risk | D1 skeleton; carve later registrations |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Options/PortalClientOptions.cs` | D1 + D2 | Yes | Safe | D2 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Persistence/DsaDbContext.cs` | D1 + D3 + D4 | Partially | High risk | D1 baseline + D3 carve |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Persistence/Migrations/DsaDbContextModelSnapshot.cs` | D2 + D3 + D4 | No (practical) | Unsafe to carve manually | Postpone; commit with owning migration batch |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Persistence/Repositories/QueueRepositories.cs` | D3 + D4 | Yes | Medium | D3 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Processing/Parsers.cs` | D3 + D4 | Yes | Medium | D3 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Processing/ProcessingServices.cs` | D3 + D4 | Partially | Medium | D3 core, D4 telemetry hooks later |
| `Backend/src/DepartmentSyncAgent.Infrastructure/RemoteAdmin/RemoteAdminServices.cs` | D2 + D4 | Yes | Medium | D4 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Services/Heartbeat/HeartbeatService.cs` | D2 + D3 + D4 | Partially | High risk | D4 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Services/Portal/ControlPlaneBootstrapService.cs` | D2 + D3 | Partially | Medium | D2 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Services/Portal/PortalClient.cs` | D2 + D3 + D4 | Partially | High risk | D2 core |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Services/Portal/PortalJobProcessor.cs` | D3 + D4 | Yes | Medium | D3 |
| `Backend/src/DepartmentSyncAgent.Infrastructure/Upload/UploadEngine.cs` | D3 + D4 | Yes | Medium | D3 |
| `Documentation/Packaging.md` | D1 + D5 | Yes | Safe | D5 |
| `Frontend/src/components/EquipmentPcSettingsPanel.tsx` | D2 + D3 | Yes | Medium | D3 |
| `Frontend/src/pages/LogsPage.tsx` | D4 + D5 | Yes | Safe | D4 |

## Dependency graph

Primary expected flow:

`D1 -> D2 -> D3 -> D4 -> D5`

Observed practical dependency edges in current tree:
- `D2 -> D1`: expected (enrollment/discovery requires host and core DI).
- `D3 -> D2`: expected (bootstrap/sync needs enrollment and portal identity).
- `D4 -> D3`: expected (diagnostics/heartbeat rely on sync state and queues).
- `D5 -> D1..D4`: expected (documentation describes implemented capabilities).

Potential circular pressure points to watch:
- `D3 <-> D4` through shared heartbeat/status/queue telemetry services.
- `D2 <-> D3` through bootstrap + portal client + sync cache contracts.

No history rewrite or code deletion is required; circles can be handled by careful ownership and staged hunk boundaries.

## Safe commitability assessment

| Capability | Self-contained commit feasible now? | Partial staging required | Whole-file required | Postpone candidates |
|---|---|---|---|---|
| D1 | Not yet (without index normalization) | High | Core startup/DI files must start here | Snapshot/migration-heavy files |
| D2 | Not yet (depends on D1 baseline extraction) | High | Enrollment/discovery controllers/services | Shared portal/heartbeat files |
| D3 | Not yet (currently over-interleaved) | Very high | Bulk sync/upload/config files | Mixed telemetry files |
| D4 | Not yet (intertwined with D3 hooks) | Medium-high | Dedicated monitoring/health files | Shared parser/queue hooks |
| D5 | Mostly yes | Low | Docs and release assets | Packaging notes tied to D1 decisions |

## Required hunk carving focus

High-priority carve files:
- `Program.cs`
- `InfrastructureServiceCollectionExtensions.cs`
- `DsaDbContext.cs`
- `IPortalClient.cs`
- `PortalClient.cs`
- `HeartbeatService.cs`

Files that should stay together in one commit:
- `DsaDbContextModelSnapshot.cs` with its owning migration group
- migration `Designer.cs` + migration `.cs` pairs
- strongly-coupled API controller + DTO + service contract sets where signatures changed together

Files that can usually be separated safely:
- Documentation under `Documentation/` and `docs/`
- workflow files under `.github/workflows/`
- isolated capability-specific controllers/services with no cross-signature edits

## Recommended commit order after normalization

1. D1 Platform Foundation
2. D2 Discovery & Provisioning
3. D3 Configuration Management
4. D4 Monitoring & Diagnostics
5. D5 Documentation & Release Assets

## Estimated difficulty

- Normalization complexity: **High**
- Carving complexity: **High** (many `MM`/`AM` cross-cap files)
- Execution risk if attempted without normalization: **High**

## Risks

- Accidental capability leakage between D1-D4 due mixed staged/unstaged states.
- Snapshot/migration drift if `DsaDbContextModelSnapshot.cs` is split incorrectly.
- Large artifact/deletion noise obscuring architectural boundaries during review.

