# Defect Workflow — Live Commissioning

When a live defect is found:

1. **Stop** the go-live path (do not continue past the failing step).
2. **Capture evidence** — CommissioningRun evidence ZIP + host logs (gateway/agent/guac).
3. **Smallest fix** justified by the evidence only.
4. **Regression test** (portal/harness).
5. **Re-run** the commissioning sequence.

## Required package fields

- Issue Summary
- Evidence (run id, ZIP path, screenshots)
- Root Cause
- Affected Components
- Fix (PR / commit)
- Regression Test
- Verification Steps
- Risk Assessment
- Recovery Procedure
