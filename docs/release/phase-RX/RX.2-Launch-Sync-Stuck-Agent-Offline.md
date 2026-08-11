# RX.2 — Launch stuck at Synchronizing input data (~49%)

**Decision:** **BLOCKED ON AGENT HEARTBEAT (DESKTOP-CSMH6BU)** — not a sync/Guacamole code stall.

## Evidence (`ra-launch-diag-31463937524.txt`)

| Item | Value |
|------|-------|
| Session | `13b9cbdd-…` **PREPARING** |
| Prepare command | `4f52ac09-…` **PENDING** (never acked) |
| Workstation | DESKTOP-CSMH6BU `last_heartbeat=None`, agent 1.0.1 |
| Workspace | sync_phase Preparing; RAW file present (`C181731.pdf`) |
| Prior session | FAILED — Preparation timeout (same PENDING prepare) |

UI ~49% = 2 done steps + 1 active (“Synchronizing input data”) in the prepare ladder.

## Root cause

Allocation/soft-online allowed session create without a live agent.  
`PREPARE_WORKSTATION` was issued but the new RAA never heartbeats / never polls commands, so input sync never completes and Guacamole cannot start.

## Code follow-up (this change)

- Session create requires **fresh heartbeat** (`workstation_healthy_for_session`)
- Clearer user-facing offline / prepare-timeout messages
- Soft-online remains for queue allocation / check-in hold only

## Operator action required

On **DESKTOP-CSMH6BU**:

1. Confirm Windows service `RemoteAnalysisAgent` is **Running** (LocalSystem, delayed auto-start)
2. Confirm outbound HTTPS to `equip.iitr.ac.in` works
3. Confirm portal shows a fresh `last_heartbeat` for this host
4. Re-open Analysis Workspace / Launch for `IICPXRD [A]202600040`

Until heartbeat is fresh, launch cannot advance past Synchronizing input data.
