# 05 — Expected Windows Filesystem

Agent root (default):

`C:\ProgramData\RemoteAnalysisAgent\`

| Path | Purpose |
|------|---------|
| `State\agent-state.json` | AgentId, portal token cache |
| `Logs\raa-*.log` | Rolling agent logs |
| `Sessions\<reservation_id>\` | Per-reservation session tree |

## Session layout (after PREPARE)

```
Sessions\<reservation_id>\
  Input\          ← files downloaded from portal RawData (and mapped inputs)
  Working\
  Output\         ← operator / analysis software places results here
  Logs\
  Temp\
  datasets\       ← mirror of Input for legacy tooling (if enabled)
```

## Expectations by workflow step

| Step | Filesystem |
|------|------------|
| After prepare + download | `Input\` contains sample file(s); SHA matches portal |
| Pause | Operator copies `sample-output.txt` into `Output\` |
| After collect | Portal has Processed copies; agent may retain until CLEAN |
| After cleanup | `Sessions\<reservation_id>\` removed (or Output retained only if defer policy); agent idle |
| Corrupt workspace | Missing session root → command fails with clear message; no silent success |
| Permission denied | ACL block on Sessions → FAILED command + ERROR status path |
| Disk full | Write fails; command FAILED; no UploadVerified |

## State after agent restart

- `agent-state.json` still present → same AgentId / token
- Missing/corrupt state → quarantine + re-register path (per agent design)
