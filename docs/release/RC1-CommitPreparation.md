# RC1 Commit Preparation — `1.0.0-RT-RC1`

**Do not execute until explicitly approved.**

---

## 1. Portal — `iic-booking-backend`

**Branch:** `release/reverse-tunnel-rc1`

### git add (include only)

```bash
git checkout -b release/reverse-tunnel-rc1

git add VERSION
git add docker-compose.ra-production.yml
git add docs/deploy/ProductionDeploymentSteps.md docs/deploy/README.md
git add docs/release/rc1/sample.env.production
git add docs/release/ReverseTunnel-RC1-Manifest.md
git add docs/release/ReverseTunnel-RC1-BOM.md
git add docs/release/ReverseTunnel-RC1-Readiness.md
git add docs/release/CompatibilityMatrix.md
git add docs/release/ReleaseChecklist.md
git add docs/release/LiveCommissioningChecklist.md
git add docs/release/RC1-BillOfMaterials.md
git add docs/release/RC1-CommitPreparation.md
git add docs/release/RELEASE-NOTES-Portal-1.0.0-RT-RC1.md
git add docs/release/RELEASE-NOTES-Gateway-1.0.0-RT-RC1.md
git add docs/release/RELEASE-NOTES-Agent-1.0.0-RT-RC1.md
git add docs/GatewayArchitecture.md docs/GatewayDeployment.md docs/GatewayScaling.md docs/MigrationGuide.md
git add docs/ReverseTunnelArchitecture.md docs/ReverseTunnelCommissioning.md docs/ReverseTunnelSAT.md
git add docs/ReverseTunnelSecurity.md docs/ReverseTunnelTroubleshooting.md
git add docs/RemoteAnalysisPhase4LiveCommissioning.md docs/RemoteAnalysisLiveCommissioning.md
git add docs/release/phase4
git add iic_booking/remote_analysis/migrations/0015_reverse_tunnel_transport.py
git add iic_booking/remote_analysis/tunnel.py iic_booking/remote_analysis/tunnel_models.py
git add iic_booking/remote_analysis/constants.py iic_booking/remote_analysis/session_models.py
git add iic_booking/remote_analysis/models.py iic_booking/remote_analysis/admin.py
git add iic_booking/remote_analysis/configuration_catalog.py
git add iic_booking/remote_analysis/guacamole/connection.py
git add iic_booking/remote_analysis/guacamole/settings_env.py
git add iic_booking/remote_analysis/operations/commissioning_observability.py
git add iic_booking/remote_analysis/operations/fault_injection.py
git add iic_booking/remote_analysis/operations/live_commissioning.py
git add iic_booking/remote_analysis/operations/live_commissioning_html.py
git add iic_booking/remote_analysis/operations/toolkit.py
git add iic_booking/remote_analysis/operations/toolkit_html.py
git add iic_booking/remote_analysis/operations/toolkit_views.py
git add iic_booking/remote_analysis/operations/views.py
git add iic_booking/remote_analysis/urls.py
git add iic_booking/remote_analysis/tests/test_reverse_tunnel.py
git add iic_booking/remote_analysis/tests/test_commissioning_toolkit.py
git add iic_booking/remote_analysis/tests/test_commissioning_observability.py
git add tests/analysis_platform/test_commissioning.py

git status
# Expected: RC1 files staged; excluded files still modified/untracked
```

### Commit

**Title:** `Release 1.0.0-RT-RC1: Reverse Tunnel Portal RC1`

**Message:**

```
Add additive reverse-tunnel transport (default direct_rdp).

Includes migration 0015, tunnel orchestrator/client, Guacamole adapter
binding, compose reverse-tunnel-gateway service, toolkit live commissioning,
and release engineering docs for platform 1.0.0-RT-RC1.

Does not enable reverse_tunnel for users. Excludes unrelated desktop CSRF
and booking-window fixes.
```

### Intentionally excluded

`config/settings/base.py`, `config/settings/local.py`, desktop CSRF files, `reservation.py`, `test_booking_analysis_window.py`, `reports/`

---

## 2. Gateway — `ReverseTunnelGateway`

**Branch:** `release/reverse-tunnel-rc1` (after first commit on `master`, or commit directly then branch)

```bash
cd ../ReverseTunnelGateway
# git already initialized (empty history)

git checkout -b release/reverse-tunnel-rc1

git add .gitignore VERSION LICENSE CHANGELOG.md README.md Dockerfile ReverseTunnelGateway.slnx
git add docs/
git add src/ tests/

git status
# Expected: all source + docs staged; bin/obj ignored

git commit   # ONLY when approved
```

**Title:** `Release 1.0.0-RT-RC1: Reverse Tunnel Gateway`

**Message:**

```
Initial Reverse Tunnel Gateway RC1 for platform 1.0.0-RT-RC1.

Admin allocate/close, guacd TCP adapter, agent WSS bridge, health/metrics,
Docker image, and release documentation.
```

### Excluded

`bin/`, `obj/`, `appsettings.Development.json`, `.vs/`

---

## 3. Agent — `RemoteAnalysisAgent`

**Branch:** `release/reverse-tunnel-rc1`

See `RemoteAnalysisAgent/docs/RC1-IncludeList.md` (groups A+B+C).

```bash
cd ../RemoteAnalysisAgent
git checkout -b release/reverse-tunnel-rc1

git add VERSION
git add src/RemoteAnalysisAgent/Tunnel/
git add tests/RemoteAnalysisAgent.Tests/TunnelFrameAgentTests.cs
git add src/RemoteAnalysisAgent/Program.cs
git add src/RemoteAnalysisAgent/RemoteAnalysisAgent.csproj
git add src/RemoteAnalysisAgent/Configuration/
git add src/RemoteAnalysisAgent/Diagnostics/
git add src/RemoteAnalysisAgent/Logging/
git add src/RemoteAnalysisAgent/Commands/CommandHandlers.cs
git add src/RemoteAnalysisAgent/Hosting/AgentOrchestratorHostedService.cs
git add src/RemoteAnalysisAgent/Hosting/LocalHealthHostedService.cs
git add src/RemoteAnalysisAgent/Options/RemoteAnalysisAgentOptions.cs
git add src/RemoteAnalysisAgent/Portal/PortalApiClient.cs
git add src/RemoteAnalysisAgent/Session/SessionServices.cs
git add src/RemoteAnalysisAgent/Workspace/WorkspaceTransferService.cs
git add src/RemoteAnalysisAgent/Workspace/PathSafety.cs
git add src/RemoteAnalysisAgent/Workspace/WorkspaceMaintenanceHostedService.cs
git add src/RemoteAnalysisAgent/appsettings.json
git add tests/RemoteAnalysisAgent.Tests/PathSafetyTests.cs
git add tests/RemoteAnalysisAgent.Tests/RemoteAnalysisAgent.Tests.csproj
git add scripts/Install-Agent.ps1 scripts/install-service.ps1 scripts/sat/
git add docs/ README.md RemoteAnalysisAgent.sln config/

git status
# Confirm appsettings.Development.json NOT staged

git commit   # ONLY when approved
```

**Title:** `Release 1.0.0-RT-RC1: Agent reverse tunnel + ship hardening`

**Message:**

```
Align Remote Analysis Agent with platform 1.0.0-RT-RC1.

Adds JOIN_TUNNEL/CLOSE_TUNNEL handlers and WSS tunnel client, plus
startup/workspace hardening required by current Program.cs DI graph.
```

### Intentionally excluded

`src/RemoteAnalysisAgent/appsettings.Development.json`, build `bin/`/`obj/`
