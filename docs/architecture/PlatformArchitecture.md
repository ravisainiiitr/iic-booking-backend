# Platform Architecture

## System Context

```mermaid
flowchart LR
  User[Portal Users\nAdmin/Lab/Operator] --> FE[Frontend]
  FE --> Portal[Portal Backend]
  Portal --> DB[(Postgres)]
  Portal --> Redis[(Redis)]
  Portal --> S3[(Object Storage)]
  Portal --> Gua[Gateway/Guacamole]
  DSA[Department Sync Agent] <--> Portal
  RAA[Remote Analysis Agent] <--> Portal
  Wizard[Equipment Wizard] <--> Portal
  RAA <--> Gua
```

## Component Architecture

```mermaid
flowchart TB
  subgraph Portal
    RA[Remote Analysis]
    DEP[Deployment Center]
    PNP[Plug-and-Play/Sync]
    LAB[Laboratory Infrastructure]
    SAT[SAT]
    DIAG[Diagnostics/Reporting]
  end

  subgraph Agents
    DSA
    RAA
    Wizard
  end

  FE[Frontend UI] --> Portal
  Portal <--> DSA
  Portal <--> RAA
  Portal <--> Wizard
  SAT --> LAB
  DIAG --> LAB
  PNP --> DEP
```

## Deployment Architecture

```mermaid
flowchart LR
  subgraph AppHost
    FE[Frontend Service]
    API[Portal API]
    WKR[Workers/Scheduler]
  end
  API --> DB[(Postgres)]
  API --> RED[(Redis)]
  API --> OBJ[(S3)]
  API --> GUA[Guacamole/Gateway]
  DSAHost[Lab Node + DSA] <--> API
  RAAHost[Analysis PC + RAA] <--> API
  RAAHost <--> GUA
```

## Core Sequence: Remote Analysis Lifecycle

```mermaid
sequenceDiagram
  participant U as User
  participant FE as Frontend
  participant P as Portal
  participant R as RAA
  participant G as Guacamole

  U->>FE: Start analysis
  FE->>P: POST session/create
  P->>R: Command poll result (launch/tunnel/session)
  R->>P: heartbeat/inventory/status
  P->>G: tunnel/session orchestration
  P-->>FE: connect/session status
  FE-->>U: session ready
  U->>FE: End analysis
  FE->>P: POST booking/session end
  P->>R: cleanup/finalize command
  R->>P: completion + upload status
```

## Core Sequence: DSA Configuration Push

```mermaid
sequenceDiagram
  participant A as Admin
  participant P as Portal
  participant D as DSA

  A->>P: Publish/update configuration
  P-->>D: Command available (poll)
  D->>P: Fetch command/config
  D->>D: Apply configuration
  D->>P: ACK + completion
  P-->>A: Updated status/health
```

## Database Ownership

| Domain | Primary app/data owner |
|---|---|
| Remote Analysis sessions/tunnel/workspaces | `remote_analysis` + related booking/equipment links |
| Equipment RA settings | `equipment` |
| Deployment metadata and compatibility | `deployment` |
| Sync templates and reservations | `sync` |
| Lab operations and SAT models | `lab_infrastructure` |

## API Ownership

| API family | Owner |
|---|---|
| `/api/v1/analysis/*` | Backend B1/B2 (+ B6/B7 overlays) |
| `/api/v1/deployment/*` | Backend B3 |
| `/api/v1/sync/*` | Backend B4 |
| `/api/v1/lab/*` | Backend B5 (+ B6/B7 overlays) |
| DSA local `api/*` | DSA D0-D4 |
| RAA runtime + installer client to portal APIs | RAA R1-R4 |

## Message Flow Summary

1. Frontend drives user-facing lifecycle via portal APIs.
2. Portal acts as control plane and source-of-truth for state, permissions, and release metadata.
3. DSA and RAA poll/report against portal endpoints for command execution and telemetry.
4. Deployment center manages installer release distribution and compatibility signaling.

## State Transition Summary

### Remote Analysis Session (high-level)
- Requested -> Reserved -> Preparing -> Ready -> Active -> Ending -> Closed
- Failure branches: PrepareFailed, LaunchFailed, UploadFailed, Timeout, CleanupPending

### Agent Command (DSA/RAA)
- Queued -> Polled -> InProgress -> Completed/Failed -> Audited

## Version Compatibility

| Component | RC1 commit chain baseline |
|---|---|
| Portal Backend | B1-B8 |
| Frontend | F1-F4 |
| DSA | D0-D4 |
| RAA | R1-R4 |
| Wizard | Deployment/DSA-associated RC1 packaging flow |

Compatibility rule: keep `/api/v1` contract stable for this RC window; additive changes only until post-RC re-baseline.
