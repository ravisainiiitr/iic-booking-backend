# Phase 4 — Live Production Commissioning & Operational Hardening

**Rule:** No new product features. Changes only from live commissioning evidence.  
**Status:** Tooling ready for live runs against AWS Portal + Reverse Tunnel + Agent + Guacamole + IIT workstation.

## Operator URLs

| Surface | Path |
|---------|------|
| Toolkit | `/api/v1/analysis/operations/toolkit/?view=html` |
| **Live Commissioning** | `/api/v1/analysis/operations/toolkit/live/?view=html` |
| Live timeline JSON | `/api/v1/analysis/operations/toolkit/live/timeline/?run_id=&booking_id=` |
| **Fault injection** | `/api/v1/analysis/operations/toolkit/faults/?view=html` |
| Start run | `POST /api/v1/analysis/operations/toolkit/runs/` |
| Evidence ZIP | `/api/v1/analysis/operations/toolkit/runs/<run_id>/evidence/` |

Color codes on Live Commissioning: **GREEN** / **AMBER** / **RED**.

## Exit criteria (must succeed without manual intervention)

Outside-IIT researcher → Login → Open completed booking → Analyze Data → Allocate workstation → Workspace prepared → Reverse tunnel established → Browser desktop opens → Operate analysis software → Save results → Upload → Cleanup → Release → Next researcher allocated. Evidence ZIP archived automatically when a commissioning run is used.

## Commissioning sequence

1. `POST …/toolkit/runs/` with workstation_id → note `commissioning_run_id`
2. Open Live Commissioning; confirm Gateway / Agents / Guacamole cards
3. Run researcher end-to-end flow on production booking
4. Load timeline with run_id + booking_id
5. Download evidence ZIP; attach gateway/agent/guac host logs into the package notes if needed
6. Fault-injection page: dry-run then inject; verify recovery checklist
7. Fill reports under `docs/release/phase4/`

## Related docs

- [Operational Readiness](release/phase4/Operational-Readiness-Report.md)
- [Go-Live Checklist](release/phase4/Go-Live-Checklist.md)
- [Rollback Checklist](release/phase4/Rollback-Checklist.md)
- [Maintenance Checklist](release/phase4/Maintenance-Checklist.md)
- [Deployment Checklist](release/phase4/Deployment-Checklist.md)
- [Recovery Validation](release/phase4/Recovery-Validation-Report.md)
- [Security Validation](release/phase4/Security-Validation-Report.md)
- [Performance Report](release/phase4/Performance-Report.md)
- [Known Issues](release/phase4/Known-Issues.md)
- [Defect Workflow](release/phase4/Defect-Workflow.md)
- [Soak Test](release/phase4/Soak-Test-24h.md)
