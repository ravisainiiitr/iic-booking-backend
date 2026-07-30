# User Acceptance Test (UAT) — Remote Analysis

**Version:** Phase 3 / Pilot  
**Prerequisites:** Administrator checklist passed; `mock_guacamole=False`; ≥2 ONLINE workstations.

Mark each case **Pass / Fail / Blocked** with evidence (screenshot or reservation/session id).

---

## Roles

| Role | Capabilities under test |
|------|-------------------------|
| Faculty | Reserve, launch session, workspace files |
| Student | Reserve within entitlement, session, cleanup |
| Lab In-charge | Assist, view ops-relevant status, help requests |
| Administrator | Manage workstations, disable PC, view audits |

---

## Test matrix

### UAT-01 Faculty happy path

| Step | Action | Expected |
|------|--------|----------|
| 1 | Faculty creates reservation for available slot | Reservation RESERVED or QUEUED→RESERVED |
| 2 | Start remote session | Session READY/TOKEN; launch URL returned |
| 3 | Open browser RDP | Desktop visible; session ACTIVE/CONNECTED |
| 4 | Upload file to workspace | File listed; checksum OK |
| 5 | End session | TERMINATED; CLEAN command completed; PC AVAILABLE |

**Result:** ☐ Pass ☐ Fail ☐ Blocked — ID: ______

### UAT-02 Student happy path

Same as UAT-01 with student account and valid booking entitlement if required.

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-03 Lab In-charge assistance

| Step | Expected |
|------|----------|
| User requests help | Assistance request created |
| Lab in-charge acknowledges/assigns | Status updates; audit/timeline entry |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-04 Administrator controls

| Step | Expected |
|------|----------|
| Disable workstation | Not allocated; status DISABLED |
| Re-enable | Eligible again |
| View audit / ops dashboard | Data present |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-05 Concurrent users

| Step | Expected |
|------|----------|
| Two users on two PCs simultaneously | Both sessions active; no cross-connect |
| Third user when pool full | Queued or clear unavailable message |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-06 Session timeout

| Step | Expected |
|------|----------|
| Idle beyond idle_timeout | Session expired/terminated; cleanup runs |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-07 Reconnect / browser refresh

| Step | Expected |
|------|----------|
| Refresh after connected | Guacamole client recovers or clear re-launch guidance; no secret leakage |
| Reuse spent launch token | Rejected |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-08 Queue handling

| Step | Expected |
|------|----------|
| All PCs busy; user reserves | QUEUED |
| PC frees | Queue promotes; user notified (portal/email) |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-09 Expired booking / entitlement

| Step | Expected |
|------|----------|
| Attempt session outside booking window | Denied with clear error |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-10 Cleanup verification

| Step | Expected |
|------|----------|
| After terminate, inspect PC | Session workspace cleaned per agent policy; processes stopped |
| Portal command history | CLEAN_* COMPLETED |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

### UAT-11 Offline agent

| Step | Expected |
|------|----------|
| Stop agent mid-idle | Workstation OFFLINE; not allocated |
| Restart | Heartbeat resumes |

**Result:** ☐ Pass ☐ Fail ☐ Blocked

---

## Sign-off

| Role | Tester | Pass count | Fail count | Date |
|------|--------|------------|------------|------|
| Faculty | | | | |
| Student | | | | |
| Lab In-charge | | | | |
| Administrator | | | | |

**UAT decision:** ☐ Accepted for pilot ☐ Accepted with waivers ☐ Rejected
