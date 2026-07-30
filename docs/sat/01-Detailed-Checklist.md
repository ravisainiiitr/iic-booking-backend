# 01 — Detailed SAT Checklist

Mark: **PASS** · **FAIL** · **N/A** · Evidence link / notes.

Legend: **A** = automated (`pytest -m sat`) · **L** = lab (`SAT_LAB=1` or manual) · **P** = performance (`SAT_PERF=1`)

---

## SAT-01 Agent Registration

| ID | Case | Type | Result | Evidence |
|----|------|------|--------|----------|
| 01.01 | First registration creates workstation + returns token once | A/L | | |
| 01.02 | Re-registration same `agentId` updates workstation (no duplicate); token omitted or rotated per policy | A/L | | |
| 01.03 | Agent restart retains identity; heartbeats resume with stored token | L | | |
| 01.04 | Lost token recovery (re-register / enrollment path) restores access | L | | |
| 01.05 | Invalid enrollment key rejected when key required | A/L | | |
| 01.06 | Duplicate workstation detection (same agentId) — no second row | A | | |

## SAT-02 Heartbeat

| ID | Case | Type | Result | Evidence |
|----|------|------|--------|----------|
| 02.01 | Normal heartbeat updates `last_heartbeat`, score, status | A/L | | |
| 02.02 | Missed heartbeats → agent offline / OFFLINE | A/L | | |
| 02.03 | Heartbeat resume → recovery to ONLINE/AVAILABLE | A/L | | |
| 02.04 | Health score responds to CPU/memory/disk/age inputs | A | | |
| 02.05 | Status transitions recorded (state history / events) | A/L | | |

## SAT-03 Workspace Lifecycle

| ID | Phase / case | Type | Result | Evidence |
|----|--------------|------|--------|----------|
| 03.01 | Preparing (Queued normalized) | A/L | | |
| 03.02 | DownloadingInput | A/L | | |
| 03.03 | VerifyingInput | A/L | | |
| 03.04 | InputReady | A/L | | |
| 03.05 | SessionStarting / SessionActive (Running) — if Guacamole path in scope | L / N/A | | |
| 03.06 | Sync-only: remain InputReady until Collect (document) | L | | |
| 03.07 | CollectingOutput | A/L | | |
| 03.08 | UploadingOutput | A/L | | |
| 03.09 | UploadVerified | A/L | | |
| 03.10 | Cleanup / Cleaning | A/L | | |
| 03.11 | Completed | A/L | | |
| 03.12 | Deleted / session folder gone | A/L | | |
| 03.13 | Failure phases: PreparationFailed, UploadFailed, RetryPending, Cancelled | A | | |

## SAT-04 File Synchronization

| ID | Case | Type | Result | Evidence |
|----|------|------|--------|----------|
| 04.01 | Single small file Input download | A/L | | |
| 04.02 | Multiple files | A/L | | |
| 04.03 | Large file >1 GB | P/L | | |
| 04.04 | Empty file (0 bytes) | A/L | | |
| 04.05 | Unicode filename | A/L | | |
| 04.06 | Long filename (near OS limit) | A/L | | |
| 04.07 | Duplicate filename policy | A/L | | |
| 04.08 | Checksum mismatch detected / failed | A/L | | |
| 04.09 | Interrupted download recovers or fails cleanly + retry | L | | |
| 04.10 | Interrupted upload recovers or fails cleanly + retry | L | | |
| 04.11 | Explicit retry succeeds | A/L | | |

## SAT-05 Remote Analysis Workflow (E2E)

| ID | Step | Type | Result | Evidence |
|----|------|------|--------|----------|
| 05.01 | Create real COMPLETED booking (RA equipment) | L | | |
| 05.02 | Create workspace (booking + workstation) | A/L | | |
| 05.03 | Upload sample input | A/L | | |
| 05.04 | Prepare workspace (PREPARE queued → completed) | A/L | | |
| 05.05 | Verify download on agent Input/ | L | | |
| 05.06 | Pause / place output manually | L | | |
| 05.07 | Collect | A/L | | |
| 05.08 | Verify Processed files + checksums | A/L | | |
| 05.09 | Cleanup | A/L | | |
| 05.10 | Workstation AVAILABLE | A/L | | |
| 05.11 | Operator can download / view result from portal | L | | |

## SAT-06 Failure Recovery

| ID | Case | Type | Result | Evidence |
|----|------|------|--------|----------|
| 06.01 | Agent crash mid-command → recoverable state | L | | |
| 06.02 | Portal restart mid-sync → agent resumes | L | | |
| 06.03 | Redis restart (if used) → queues/sessions recover | L / N/A | | |
| 06.04 | Network interruption → retry / offline | L | | |
| 06.05 | Database restart → no corruption | L | | |
| 06.06 | Partial upload → incomplete not marked verified | A/L | | |
| 06.07 | Disk full (agent or portal) → safe failure | L | | |
| 06.08 | Permission denied on session path | L | | |
| 06.09 | Corrupt workspace / missing folder | A/L | | |

## SAT-07 Security

| ID | Case | Type | Result | Evidence |
|----|------|------|--------|----------|
| 07.01 | Anonymous → denied (JSON) / login redirect (HTML) | A | | |
| 07.02 | Normal user → no manage APIs | A | | |
| 07.03 | Department Admin → manage allowed per RBAC | A | | |
| 07.04 | Remote Analysis Manager → manage allowed | A | | |
| 07.05 | Super Admin → manage allowed | A | | |
| 07.06 | Expired agent token rejected | A | | |
| 07.07 | Invalid portal / agent token rejected | A | | |
| 07.08 | CSRF enforced for session POSTs | A | | |
| 07.09 | Session hijack attempt (stolen cookie wrong origin / invalid session) | L | | |
| 07.10 | Query `?token=` does not auth JSON API | A | | |

## SAT-08 Performance

| ID | Case | Type | Result | Evidence |
|----|------|------|--------|----------|
| 08.01 | 100 MB file transfer within baseline | P | | |
| 08.02 | 1 GB file transfer within baseline | P | | |
| 08.03 | 10 simultaneous workspaces | P | | |
| 08.04 | 20 workstations heartbeats | P | | |
| 08.05 | Heartbeat under load (p95 latency) | P | | |
| 08.06 | Upload throughput recorded | P | | |

## SAT-09 Database Integrity

| ID | Case | Type | Result | Evidence |
|----|------|------|--------|----------|
| 09.01 | Model updates match phase transitions | A | | |
| 09.02 | Timestamps monotonic / set (`created_at`, `last_synced_at`, `upload_verified_at`) | A | | |
| 09.03 | Foreign keys intact (workspace↔reservation↔workstation↔booking) | A | | |
| 09.04 | Cleanup leaves no orphan session files / dangling PENDING commands | A/L | | |
| 09.05 | Soft-deleted files not served as active Input | A | | |

## SAT-10 Audit

| ID | Case | Type | Result | Evidence |
|----|------|------|--------|----------|
| 10.01 | Agent registration / auth events | A/L | | |
| 10.02 | Portal manage actions (commissioning) | A | | |
| 10.03 | Workspace lifecycle audits | A | | |
| 10.04 | Upload / download / collect | A/L | | |
| 10.05 | Cleanup | A/L | | |
| 10.06 | Authentication failures logged | A/L | | |
| 10.07 | Authorization denials observable | A/L | | |

---

## Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| SAT lead | | | |
| Portal eng | | | |
| Agent eng | | | |
| Security | | | |
| Ops | | | |
