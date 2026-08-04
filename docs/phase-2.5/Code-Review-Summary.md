# Code Review Summary — Phase 2.5

**Date:** 2026-08-04  
**Scope:** Stabilization pass across Phase 1 Plug-and-Play and Phase 2 Enterprise Lifecycle  
**Rule:** Defect fixes only — no new business features

---

## 1. Review objective

Confirm that Phase 2.5 fixes are minimal, correct, and backward-compatible; identify remaining tech debt; align with SAT catalog and production readiness gates.

---

## 2. Modules reviewed

| Module / package | Files reviewed | Focus |
|------------------|----------------|-------|
| `iic_booking/sync` | `serializers.py`, `services/bootstrap.py`, `services/heartbeat.py`, `models.py` | C-01 heartbeat payload; H-12 bootstrap timing |
| `iic_booking/lab_infrastructure` | `views.py`, `services/fleet.py`, `services/testing.py`, `models.py`, `tasks.py` | C-02 config push; H-09 node detail; test dashboard catalog |
| `iic_booking/deployment` | `views.py`, `models.py`, `management/commands/publish_*.py` | Deployment Center releases, wizard tickets |
| `iic_booking/remote_analysis` | `services/registration.py`, `services/heartbeat.py`, `services/commands.py`, `installer/views.py`, `tunnel.py`, `views.py` | H-05/H-07/H-08; reverse tunnel |
| `iic_booking/equipment` | `remote_analysis_integration/*`, `models.py` | RA booking integration; session/checkin fields |
| `config` | `api_router.py`, `settings/base.py` | URL wiring; app registration |

**Out of repo (referenced):** DSA local API (pairing, H-01/H-02/H-04), Equipment PC Wizard, RAA agent binaries.

---

## 3. Critical fixes — review notes

### C-01 — equipment_pcs heartbeat rollup

**Problem:** Portal heartbeat serializer dropped `equipment_pcs[]`, breaking EqPC visibility under DSA nodes.

**Fix:** Preserve array field through `HeartbeatRequestSerializer` validation and persistence.

**Review:** Targeted change; no schema migration required. Backward compatible — older agents omitting field still valid.

**Smell fixed:** Silent field stripping in nested serializer `to_internal_value`.

---

### C-02 — Config push/rollback persistence

**Problem:** Lab configuration views bumped `configuration_version` without saving full profile snapshot fields applied from templates.

**Fix:** Push and rollback paths persist complete profile state (folders, policies, software requirements, etc.).

**Review:** Aligns Portal with DSA bootstrap contract. Rollback must remain atomic — verify transaction wrapping in lab tests.

**Smell fixed:** Version-only update anti-pattern masking incomplete state writes.

---

## 4. High-priority fixes — review notes

| ID | Area | Fix summary | Review outcome |
|----|------|-------------|----------------|
| H-01 | DSA pairing | Fail-closed when ManagementApiKey unset | Correct secure default |
| H-02 | ConfigJson OTP | Strip OTP after validation | Reduces secret exposure |
| H-04 | Status ingest | Auth required off loopback | Closes forgery vector |
| H-05 | Repair dispatch | Route RestartAgent to correct agent type | Logic branch fix; add regression test |
| H-07/H-08 | RAA updates | Agent/enrollment auth on discover/report | Removes admin-only bottleneck |
| H-09 | Fleet detail | Scoped queries vs full tree rebuild | Performance + correctness |
| H-12 | Bootstrap | Defer `last_reported_*` until apply succeeds | Prevents false drift detection |

---

## 5. Code smells addressed

| Smell | Location | Resolution |
|-------|----------|------------|
| Silent serializer field drop | sync serializers | Explicit fields / nested handling for equipment_pcs |
| Partial model updates on config lifecycle | lab views | Full field copy on push/rollback |
| Admin-only agent endpoints | RA installer views | Agent-scoped permissions |
| O(n) fleet tree on detail | fleet service | Node-scoped selectors |
| Premature state flags | bootstrap service | Set reported fields after ack path |
| Open pairing when misconfigured | DSA (external) | Fail-closed |

---

## 6. Remaining tech debt

| ID | Description | Severity | Recommendation |
|----|-------------|----------|----------------|
| H-06 | DSA command handler completeness for restart/upgrade | High | Agent-side audit; integration test SAT-FLT-003 on DSA |
| H-10 | N+1 on fleet list/detail at scale | Medium–High | Add `select_related` / prefetch; annotated compliance queries |
| H-11 | Diagnostics vs full commissioning depth | Medium | Document scope; optional expanded commissioning command |
| — | Per-software `exists()` in allocation | Medium | Batch annotate for 50+ PC fleets |
| — | Duplicate workstation cleanup | Medium | One-time admin command + fingerprint enforcement |
| — | Wizard elevation stubs | Medium | Complete UAC elevation path or document manual steps |
| — | mTLS | Low (deferred) | Future transport hardening |
| — | Guacamole recording | N/A | Feature not implemented |

---

## 7. Test coverage notes

| Area | Coverage | Gap |
|------|----------|-----|
| lab_infrastructure | `tests/test_lab_infrastructure.py` | Live multi-node SAT |
| remote_analysis SAT | `tests/sat/*` | Lab agent E2E |
| reverse tunnel | `tests/test_reverse_tunnel.py` | Gateway outage FAIL-003 |
| sync heartbeat | Unit tests partial | equipment_pcs integration |
| deployment | Manual | SAT-DEP installer flows |

**Catalog sync:** `iic_booking.lab_infrastructure.services.testing.SAT_CATALOG` mirrors [SAT-Master-Test-Plan.md](./SAT-Master-Test-Plan.md) — keep in sync on test ID changes.

---

## 8. Backward compatibility

| Change | Breaking? | Notes |
|--------|-----------|-------|
| Heartbeat equipment_pcs preserved | No | Additive for Portal; DSA already sent field |
| Config push full persist | No | Stricter correctness; DSA bootstrap unchanged contract |
| Pairing fail-closed | **Behavioral** | Labs without ManagementApiKey must configure key — intentional |
| OTP stripped from ConfigJson | No | Removes sensitive data; wizard re-issue if needed |
| Loopback auth | **Behavioral** | Unauthenticated remote status posts now fail — intended |
| Agent update auth | **Behavioral** | Scripts using admin session for discover must use agent token |
| Node detail query | No | API response shape unchanged |

**Migration heads (apply in order):**

- `deployment.0002_compatibility_repair_packages`
- `sync.0018_equipment_pc_ip_reservation`
- `lab_infrastructure.0001_initial`
- `remote_analysis.0020_reservation_checkin_window` (and prior rt-port chain)

Downgrade not supported for production — forward-only deploy.

---

## 9. Security review highlights

- Fail-closed defaults preferred over warn-only (H-01, H-04).
- Secrets stripped at earliest persistence boundary (H-02).
- Agent-facing endpoints use agent credentials, not session admin (H-07/H-08).
- Config signatures remain HMAC-SHA256 per [`docs/enterprise/ConfigurationPush.md`](../enterprise/ConfigurationPush.md).

Full matrix: [Security-Test-Plan.md](./Security-Test-Plan.md).

---

## 10. Documentation alignment

| Doc set | Status |
|---------|--------|
| `docs/plug-and-play/` | Phase 1 accurate |
| `docs/enterprise/` | Phase 2 accurate |
| `docs/sat/` | RA SAT valid; extended by Phase 2.5 |
| `docs/phase-2.5/` | This deliverable set |

---

## 11. Reviewer recommendations

1. Add regression test asserting `equipment_pcs` round-trip on sync heartbeat (C-01).
2. Add lab infrastructure test for rollback field equality pre/post (C-02).
3. Track H-06 in agent repository with linked SAT-FLT-003 evidence.
4. Profile fleet API with django-silk before declaring PERF pass at 50 nodes.
5. Avoid broad refactors during SAT — defect fixes only per Phase 2.5 rule.

---

## 12. Sign-off

| Reviewer | Focus area | Date | Result |
|----------|------------|------|--------|
| Portal lead | sync, lab_infrastructure, deployment | | |
| RA lead | remote_analysis, tunnel, installer | | |
| Security | auth, pairing, secrets | | |

**Overall code review status:** **Approved for SAT execution** — Critical fixes accepted; High pending items documented; no blocking review findings on resolved Criticals.
