# Production Acceptance Criteria

Allowed outcomes: `PASS` / `FAIL` / `BLOCKED` / `NOT APPLICABLE`.

| Capability | Acceptance Criteria (Measurable) | Outcome | Evidence | Notes |
|---|---|---|---|---|
| Authentication | 100% of tested valid users login; invalid credentials rejected |  |  |  |
| Authorization | Role-gated endpoints deny unauthorized roles in all sampled tests |  |  |  |
| Booking lifecycle | Booking create/approve/cancel flows complete without data inconsistency |  |  |  |
| Reservation and check-in | Reservation/check-in transitions complete within SLA |  |  |  |
| Remote analysis launch | Session create->launch->connect success rate meets target |  |  |  |
| End analysis | End analysis performs cleanup and state closure consistently |  |  |  |
| Upload/download | Artifact upload/download complete with audit trace |  |  |  |
| DSA enrollment | New DSA enrolls and heartbeats within target time |  |  |  |
| RAA enrollment | New RAA registers and command-polls successfully |  |  |  |
| Configuration push | Config push ACK success ratio meets target |  |  |  |
| Deployment center | Installer metadata and ticket download validated for all artifacts |  |  |  |
| SAT dashboard | SAT runs, evidence upload, readiness calculations succeed |  |  |  |
| Alerts and logging | Critical events generate alerts and searchable logs |  |  |  |
| Backup and rollback | Restore test succeeds and services return to known-good state |  |  |  |
| Performance baseline | Key APIs and workflows meet agreed p95 response targets |  |  |  |
| Security baseline | No critical unresolved security finding in commissioning window |  |  |  |

## Decision Rule

- **GO**: All critical criteria `PASS`; no critical `FAIL`/`BLOCKED`.
- **Conditional GO**: Non-critical failures only with approved mitigation and timeline.
- **NO GO**: Any critical `FAIL`/`BLOCKED` without approved mitigation.
