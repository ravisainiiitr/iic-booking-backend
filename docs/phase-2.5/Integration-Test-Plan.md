# Integration Test Plan — Phase 2.5

**Date:** 2026-08-04  
**Scope:** Cross-component sequences validating API chains, event ordering, and data consistency across Portal, DSA, Equipment PC, RAA, Guacamole, and PostgreSQL.

Integration tests prove that independently correct modules work together. Detailed RA API sequences remain in [`docs/sat/03-Expected-API-Sequence.md`](../sat/03-Expected-API-Sequence.md).

---

## 1. Integration architecture

```
┌─────────────┐     heartbeat      ┌─────────────┐    status :6001    ┌──────────────┐
│   Portal    │◄──────────────────│     DSA     │◄───────────────────│ Equipment PC │
│  (Django)   │──── bootstrap ───►│  (dept LAN) │──── config-pack ──►│   (Wizard)   │
└──────┬──────┘                   └─────────────┘                    └──────────────┘
       │
       │ heartbeat / commands / workspace
       ▼
┌─────────────┐     reverse tunnel    ┌──────────────┐
│     RAA     │◄─────────────────────►│  Guacamole   │
│ (Analysis)  │                       │   gateway    │
└─────────────┘                       └──────────────┘
```

**Rule:** Equipment PCs never call Portal directly. All EqPC visibility flows DSA → Portal heartbeat rollup.

---

## 2. Test harness

| Mode | Tooling | When to use |
|------|---------|-------------|
| Automated | `pytest` — `remote_analysis/tests/`, `lab_infrastructure/tests/` | CI on every RC |
| Semi-auto | Postman/curl scripts + DB assertions | Staging smoke |
| Live lab | Real agents on VLAN | Pre-production sign-off |

Record correlation IDs: `agent_id`, `workstation_id`, `booking_id`, `workspace_uuid`, `configuration_version`.

---

## 3. Integration scenarios

### INT-01 — DSA registration and Portal visibility

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | DSA | `POST /api/v1/sync/agents/register/` (or bootstrap path) | Agent row created; token issued |
| 2 | DSA | Periodic `POST /api/v1/sync/agents/heartbeat/` | `last_seen` updated; status Online |
| 3 | Portal | `GET /api/v1/lab/infrastructure/` | DSA node present under department |
| 4 | DB | `sync_departmentsyncagent` | No duplicate agent for same fingerprint |

**Pass criteria:** Heartbeat within configured interval; fleet node matches DSA hostname/IP.

---

### INT-02 — Equipment PC announce → equipment_pcs rollup (C-01 fix)

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | Wizard | `POST /api/pairing/issue` (with ManagementApiKey) | Pairing token TTL ~15 min |
| 2 | Wizard | `POST /api/equipment-pcs/announce` | Local DSA registration upserted |
| 3 | EqPC agent | Status post to DSA `:6001` | DSA local SQLite updated |
| 4 | DSA | Heartbeat payload includes `equipment_pcs[]` | Serializer preserves array (C-01) |
| 5 | Portal | Lab infrastructure tree | EqPC child under DSA with live status |

**Pass criteria:** EqPC visible within one heartbeat cycle; MAC/MachineGuid stable on re-announce.

---

### INT-03 — Configuration push end-to-end (C-02 fix)

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | Main Admin | Apply `EquipmentSyncTemplate` → profile | `configuration_version` bumped |
| 2 | Portal | Profile fields persisted (not version alone) | Full snapshot in DB (C-02) |
| 3 | Portal | Set `bootstrap_required=True` on DSA | Flag visible on agent row |
| 4 | DSA | Bootstrap fetch | Document includes signature, folders, policies |
| 5 | DSA | Push to EqPC; EqPC applies | Local folders/policy updated |
| 6 | DSA | `POST /api/v1/lab/configuration/ack/` | Ack row Applied |
| 7 | Portal | Lab UI config status | Applied + version match |

**Pass criteria:** Rollback (INT-04) restores prior snapshot; ack idempotent (SAT-API-002).

---

### INT-04 — Configuration rollback

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | Main Admin | `POST .../configuration/profiles/{id}/rollback/` | Prior version restored to profile |
| 2 | System | `configuration_version` incremented | DSA sees bootstrap_required |
| 3 | DSA | Re-bootstrap + EqPC apply | EqPC matches rolled-back policy |
| 4 | Portal | Ack + dashboard | Applied status for new version |

---

### INT-05 — Booking → raw sync → workspace prepare

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | User | Create booking → approval → sample accept | Booking state advances |
| 2 | Operator | Accept sample | Prepare triggers on Analysis side |
| 3 | Portal | `PREPARE` command to RAA | Command row queued |
| 4 | RAA | Download input / verify | Workspace phases advance |
| 5 | DSA | Sync raw from EqPC share | Portal media/SyncLog updated |
| 6 | DB | Booking + workspace + sync tables | FK integrity; no orphan workspace |

Reference: [`docs/sat/04-Expected-Database-Changes.md`](../sat/04-Expected-Database-Changes.md).

---

### INT-06 — RA session + reverse tunnel + Guacamole

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | Portal | Allocate workstation (software filter) | AVAILABLE → BUSY |
| 2 | Portal | `JOIN_TUNNEL` / session create | Tunnel record ACTIVE |
| 3 | RAA | Open reverse tunnel to gateway | Heartbeat reports tunnel status |
| 4 | User | Open Guacamole URL from booking | Desktop connects < SLA |
| 5 | User | End Analysis | Cleanup command; tunnel closed |
| 6 | Portal | Workstation | Returns AVAILABLE or CLEANING → AVAILABLE |

Reference: [`docs/ReverseTunnelArchitecture.md`](../ReverseTunnelArchitecture.md).

---

### INT-07 — Agent update discover/report (H-07/H-08 fix)

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | RAA | `GET /api/v1/analysis/updates/discover/` with agent auth | Update manifest returned |
| 2 | RAA | Download update package | Ticket or signed URL valid |
| 3 | RAA | `POST /api/v1/analysis/updates/report/` | Report stored; not admin-only |
| 4 | Portal | Fleet node detail | Version reflects report |

**Pass criteria:** Unauthenticated discover/report rejected; enrollment/agent token accepted.

---

### INT-08 — Lab repair command dispatch (H-05 fix)

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | Main Admin | `POST /api/v1/lab/infrastructure/nodes/{id}/repair/` `RestartAgent` | Command queued for correct agent type |
| 2 | Agent | Poll commands; execute restart | Service restarts; heartbeat resumes |
| 3 | Portal | Audit log | Repair action recorded |
| 4 | Portal | Node detail | Does not rebuild full tree on each poll (H-09) |

**Note:** DSA restart/upgrade command completeness tracked under H-06 (pending).

---

### INT-09 — Health detectors → alerts → email

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | Ops | Stop DSA service | Heartbeats miss threshold |
| 2 | Celery | `run_lab_health_detectors` | Detector flags offline |
| 3 | Portal | `GET /api/v1/lab/alerts/` | Critical alert row |
| 4 | Email | Notification task | Email to configured recipients |
| 5 | Ops | Restart DSA | Alert clears or resolves |

---

### INT-10 — Pairing and loopback security (H-01, H-04)

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | Attacker | `POST /api/pairing/issue` without ManagementApiKey | 403 (H-01) |
| 2 | Attacker | `POST` EqPC status to DSA from remote IP without token | 401/403 (H-04) |
| 3 | Wizard | Valid pairing token | Succeeds from LAN |
| 4 | DB | ConfigJson after validate | OTP stripped (H-02) |

---

### INT-11 — Bootstrap identity (H-12 fix)

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | DSA | First bootstrap after install | `last_reported_*` not prematurely set |
| 2 | DSA | First successful heartbeat after apply | Reported fields match actual apply |
| 3 | Portal | Config drift detector | No false drift on fresh install |

---

### INT-12 — Portal + DB restart mid-operation

| Step | Actor | Call / event | Expected chain |
|------|-------|--------------|----------------|
| 1 | System | Active sync or heartbeat in flight | — |
| 2 | Ops | Restart Gunicorn/uWSGI + PostgreSQL | Brief outage |
| 3 | Agents | Retry heartbeat/bootstrap | Exponential backoff; no duplicate agents |
| 4 | DB | Transaction integrity | No partial workspace without recovery path |

---

## 4. Event ordering matrix

| Domain event | Must precede | Must follow (eventually) |
|--------------|--------------|--------------------------|
| DSA register | — | heartbeat Online |
| Config version bump | template apply | bootstrap_required |
| Bootstrap ack | EqPC apply | Applied status |
| Sample accept | booking approved | raw sync |
| PREPARE complete | input on disk | InputReady |
| JOIN_TUNNEL | allocation | Guacamole connect |
| End Analysis | session active | cleanup + tunnel close |

---

## 5. Database consistency checks

After each INT scenario, verify:

| Check | Query / inspection |
|-------|-------------------|
| No orphan workspaces | Workspace without booking/reservation resolved or archived |
| No stuck BUSY workstations | BUSY without active reservation/session > timeout |
| No ACTIVE tunnels without session | Orphan tunnel cleanup |
| Config ack matches version | Ack.version == agent.configuration_version |
| Audit completeness | Repair, config push, session lifecycle events present |

---

## 6. Automated test map

| Scenario | Test module |
|----------|-------------|
| INT-01/02 | `lab_infrastructure/tests/test_lab_infrastructure.py` |
| INT-03/04 | Config push tests + manual SAT-DSA-002/003 |
| INT-05/06 | `remote_analysis/tests/sat/test_sat_05_workflow.py` |
| INT-07 | Agent update auth tests |
| INT-08 | Repair action tests |
| INT-10 | `remote_analysis/tests/test_reverse_tunnel.py`, security SAT |
| INT-12 | `remote_analysis/tests/sat/test_sat_06_recovery.py` |

---

## 7. Entry / exit criteria

**Entry:** Staging with all agents enrolled; migrations current; correlation logging enabled.

**Exit:**

- [ ] All INT-01 … INT-12 executed in lab with PASS
- [ ] No unresolved Critical integration defects
- [ ] DB consistency checks pass after failure scenarios (INT-12)

---

## 8. Execution log template

| Scenario | Date | Environment | SHA | Result | Notes |
|----------|------|-------------|-----|--------|-------|
| INT-01 | | | | | |
| INT-02 | | | | | |
| … | | | | | |
