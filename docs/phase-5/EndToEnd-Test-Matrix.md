# End-to-End Test Matrix

Status values: `PASS` / `FAIL` / `BLOCKED` / `NOT APPLICABLE`.

| ID | Workflow | Systems Involved | Pre-Conditions | Expected Outcome | Status | Evidence |
|---|---|---|---|---|---|---|
| E2E-001 | User Registration | Frontend, Portal, SMTP | Email service configured | User created and verification mail sent |  |  |
| E2E-002 | Wallet Flow | Frontend, Portal, Payment integration | User account active | Wallet credit/debit reflected correctly |  |  |
| E2E-003 | Booking Creation | Frontend, Portal, Postgres | Equipment available | Booking created with valid state |  |  |
| E2E-004 | Booking Approval | Portal, Admin workflow | Approval role account | Booking approved and audit logged |  |  |
| E2E-005 | Payment Flow | Frontend, Portal, Payment integration | Billable booking | Payment success/failure handled correctly |  |  |
| E2E-006 | Slot Allocation | Portal scheduler, Equipment model | Eligible time slots exist | Correct slot assigned with conflict prevention |  |  |
| E2E-007 | Sample Submission | Frontend, Portal | Booking in valid stage | Sample metadata persisted and traceable |  |  |
| E2E-008 | Remote Analysis Launch | Frontend, Portal, RAA, Guacamole | Reservation checked-in | Session created, launched, connect token valid |  |  |
| E2E-009 | Software Allocation | Portal, DSA inventory/mapping | Software map configured | Workstation selection respects software requirements |  |  |
| E2E-010 | Queue Handling | Portal scheduler/queue | No immediate capacity | Waitlist queue updates and dequeue behavior valid |  |  |
| E2E-011 | Maintenance Mode | Portal, Lab infra, Scheduler | Node selected | Node excluded/included according to maintenance status |  |  |
| E2E-012 | Calibration Workflow | Portal, Equipment model | Calibration policy enabled | Calibration constraints enforced in booking flow |  |  |
| E2E-013 | Result Upload | RAA/DSA, Portal, S3 | Session ended with artifacts | Upload succeeds and metadata linked to booking |  |  |
| E2E-014 | Result Download | Frontend, Portal, S3 | Result exists | Authorized download works and audit captured |  |  |
| E2E-015 | Email Notifications | Portal, SMTP | SMTP credentials valid | Event notifications delivered and logged |  |  |
| E2E-016 | S3 Archive/Restore | Portal, S3 | Object storage configured | Archive and retrieval both succeed |  |  |
| E2E-017 | Deployment Center Publish | Portal Deployment Center | Release metadata prepared | Release listed with compatibility fields |  |  |
| E2E-018 | Equipment Wizard Provisioning | Wizard, DSA, Portal | Equipment PC reachable | Discovery/pair/provision flow completes |  |  |
| E2E-019 | DSA Discovery | DSA, Portal | DSA enrolled | Equipment discovery posted to portal correctly |  |  |
| E2E-020 | Configuration Push | Portal, DSA, Equipment PCs | Config profile published | Push received, applied, ACK posted |  |  |
| E2E-021 | Repair Package Execution | Deployment Center, DSA/RAA | Repair package available | Repair package downloaded and applied safely |  |  |
| E2E-022 | Rollback Drill | Portal, DB, Deployment artifacts | Backup snapshot captured | Services restored to known good state |  |  |
