# Rollback Checklist — Remote Analysis

Use only if go-live fails exit criteria or a P0 appears.

1. [ ] Freeze new analysis launches (ops announcement)
2. [ ] Set Portal `transport_mode=direct_rdp` **only if** lab network path still valid; otherwise disable remote analysis feature flag / equipment `enable_remote_analysis`
3. [ ] Drain active tunnels via Toolkit / TunnelOrchestrator close
4. [ ] Confirm workstations released (no orphan reservations)
5. [ ] Preserve evidence ZIPs and gateway/agent/guac logs
6. [ ] Redeploy last known-good Portal image/tag
7. [ ] Verify booking portal core (non-analysis) still healthy
8. [ ] File defect package per Defect-Workflow.md

**Do not** redesign tunnel/booking during rollback — restore and stabilize first.
