# Build Verification — Phase 2.7 Step 6

**Date:** 2026-07-28  
**Scope:** Compile/build only. No commits. No business-logic changes.

| Component | Command | Result | Notes |
|-----------|---------|--------|-------|
| RAA | `dotnet build RemoteAnalysis.Agent.slnx -c Release` | **PASS** | 0 warnings, 0 errors |
| DSA API | `dotnet build DepartmentSyncAgent.Api.csproj -c Release` | **PASS** | NU1903 (SQLitePCLRaw) + obsolete API warnings |
| Equipment Wizard | `dotnet build EquipmentPcConfigurationWizard.csproj -c Release` | **PASS** | 0 warnings, 0 errors |
| Portal Frontend | `npm run build` (Vite) | **PASS** | Large chunk warning only |
| Portal Backend | Full Docker/`uv` image build | **SKIPPED** | No `uv`/`.venv` on this machine |

### Backend note

Light `python -m compileall` may be recorded separately if a system Python is available. Full Django runtime verification remains deferred to Lab SAT / Docker host.

### Side effect of verification

Successful .NET and Vite builds **recreated** regenerable outputs:

- DSA / Wizard / RAA `bin/` + `obj/`
- Frontend `dist/`

These are gitignored. **Await confirmation** before deleting them again (same policy as Step 2). Local `artifacts/` was **not** deleted.

### Artifacts still awaiting confirmation

| Path | Action pending |
|------|----------------|
| `DepartmentSyncAgent/artifacts/` | Confirm delete or keep offline |
| Backend `tmp_commission_run.py` | Confirm remove from index / disk |
| Post-verify `bin/`/`obj/`/`dist/` | Confirm re-clean |
