# 12 — Live Commissioning Report

**Fill only after a successful (or formally aborted) live run.**  
Do not invent data. Leave blank until observed.

| Field | Value |
|-------|-------|
| Report date | |
| Portal SHA | |
| Agent version | |
| Analysis PC hostname / agent_id | |
| Operator | |
| Booking ID | |
| Workspace ID | |
| Reservation ID | |
| Outcome | ☐ PASS · ☐ FAIL (aborted) |

---

## 1. Timeline of each phase

| Phase | Start (UTC) | End (UTC) | Duration | Result |
|-------|-------------|-----------|----------|--------|
| Toolkit connectivity | | | | |
| Toolkit self-test | | | | |
| Booking created | | | | |
| Workspace created | | | | |
| Input upload (portal) | | | | |
| Prepare / Input download | | | | |
| Input verification | | | | |
| Analysis (manual/software) | | | | |
| Collect / Output upload | | | | |
| Checksum verification | | | | |
| Portal result download | | | | |
| Cleanup | | | | |
| Workstation AVAILABLE | | | | |

---

## 2. File transfer durations

| Transfer | Size | SHA-256 | Duration | Notes |
|----------|------|---------|----------|-------|
| Input portal → agent | | | | |
| Output agent → portal | | | | |

---

## 3. Checksum verification

| File | Portal SHA-256 | Agent / peer SHA-256 | Match |
|------|----------------|----------------------|-------|
| Input | | | ☐ |
| Output | | | ☐ |

---

## 4. Defects encountered

| ID | Step | Severity | Summary | Fixed in commit | Regression test |
|----|------|----------|---------|-----------------|-----------------|
| | | | | | |

_None if empty._

---

## 5. Fixes applied

| Commit | Description |
|--------|-------------|
| | |

---

## 6. Final production readiness assessment

| Question | Answer |
|----------|--------|
| Ready for limited production use on this PC? | ☐ Yes · ☐ No · ☐ Yes with waivers |
| Open S1/S2 defects? | |
| Waivers (if any) | |
| Next action | |

**Sign-off**

| Role | Name | Date | Sign |
|------|------|------|------|
| Operator | | | |
| Engineering | | | |
| SAT lead | | | |

---

## Related

- Runbook: [../RemoteAnalysisLiveCommissioning.md](../RemoteAnalysisLiveCommissioning.md)
- SAT-05 checklist: [01-Detailed-Checklist.md](01-Detailed-Checklist.md)
- Toolkit guide: [../RemoteAnalysisCommissioningToolkit.md](../RemoteAnalysisCommissioningToolkit.md)
