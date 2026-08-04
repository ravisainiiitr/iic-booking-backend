# Performance Test Plan — Phase 2.5

**Date:** 2026-08-04  
**Scope:** Staging load and soak tests for Portal aggregate APIs, heartbeats, sync, and booking scheduler under Phase 1/2 fleet sizes.

Performance testing validates scalability assumptions documented in Phase 2.5 stabilization. No new optimization features are in scope — measure, document, and gate on agreed thresholds.

Related baselines: [`docs/sat/08-Performance-Baseline.md`](../sat/08-Performance-Baseline.md).

---

## 1. Objectives

1. Establish staging baselines for lab infrastructure APIs at 10, 50, and 100 simulated nodes.
2. Verify heartbeat ingestion and DSA sync do not drop critical updates under burst load.
3. Characterize booking scheduler behavior under concurrent booking attempts.
4. Identify N+1 query hotspots (H-10) for post-Phase-2.5 tuning.

---

## 2. Environments

| Tier | Portal | Database | Agents | Purpose |
|------|--------|----------|--------|---------|
| **Perf-A** | Staging (dedicated) | PostgreSQL (production-like size) | Simulated heartbeats | API p95 |
| **Perf-B** | Staging | Same | 5–20 live agents | Heartbeat + sync |
| **Perf-C** | Staging | Same | Live + k6/Locust | Scheduler stress |

**Isolation:** Do not run destructive perf tests against production.

---

## 3. Metrics summary

| Metric ID | Description | Target | Measured | Pass criteria | Status |
|-----------|-------------|--------|----------|---------------|--------|
| PERF-API-01 | `GET /api/v1/lab/infrastructure/` p50 | < 500 ms @ 50 nodes | | p50 ≤ target | |
| PERF-API-02 | `GET /api/v1/lab/infrastructure/` p95 | < 2000 ms @ 50 nodes | | p95 ≤ target (SAT-PERF-001) | |
| PERF-API-03 | `GET /api/v1/lab/infrastructure/nodes/{id}/` p95 | < 800 ms | | p95 ≤ target (H-09 scoped detail) | |
| PERF-API-04 | `GET /api/v1/lab/alerts/` p95 | < 1000 ms | | p95 ≤ target | |
| PERF-API-05 | `GET /api/v1/lab/software/compliance/` p95 | < 1500 ms @ 50 nodes | | p95 ≤ target | |
| PERF-HB-01 | Sync heartbeat ingest throughput | ≥ 200 req/min sustained | | No 5xx; lag < 2× interval | |
| PERF-HB-02 | Burst 2× agents heartbeat same minute | No dropped rollups | | All agents update `last_seen` | |
| PERF-SYNC-01 | Concurrent raw file sync (5 EqPC) | All complete < 10 min | | SyncLog success ≥ 99% | |
| PERF-RA-01 | Concurrent RA session start (5 PCs) | All connect < 60 s | | No allocation deadlock | |
| PERF-SCH-01 | 100 concurrent booking creates | < 5% 409/validation fail (expected contention) | | No 5xx; DB stable | |
| PERF-SCH-02 | Scheduler queue 50 waiting | Drain within 15 min after capacity | | Queue fairness documented | |
| PERF-DB-01 | Migration apply time (full chain) | < 10 min staging | | Completes without lock timeout | |
| PERF-UI-01 | Lab Infrastructure first paint | < 3 s on admin LAN | | Lighthouse/TTFB acceptable | |

---

## 4. Baseline placeholders

Record measured values after each perf run. Replace `TBD` with numbers and environment notes.

### 4.1 Lab infrastructure API (50 nodes simulated)

| Concurrent users | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Error rate | Date | SHA |
|------------------|-----|----------|----------|----------|------------|------|-----|
| 1 | TBD | TBD | TBD | TBD | TBD | | |
| 5 | TBD | TBD | TBD | TBD | TBD | | |
| 10 | TBD | TBD | TBD | TBD | TBD | | |

### 4.2 Heartbeat + sync (live agents)

| Agents | Interval (s) | Duration (min) | Missed heartbeats | Sync failures | Notes |
|--------|--------------|----------------|-------------------|---------------|-------|
| 5 | 60 | 30 | TBD | TBD | |
| 20 | 60 | 30 | TBD | TBD | |

### 4.3 Booking scheduler stress

| Scenario | Bookings | Equipment | Result | Notes |
|----------|----------|-----------|--------|-------|
| Concurrent create | 100 | 10 RA-enabled | TBD | |
| Queue wait | 50 | 2 Analysis PCs | TBD | |

---

## 5. Test procedures

### PERF-RUN-01 — Lab tree at scale (SAT-PERF-001)

**Preconditions:** Seed or simulate ≥50 department nodes (DSA + EqPC + Analysis PC mix).

**Steps:**

1. Warm up with 50 sequential GETs (discard).
2. Run k6/Locust: 10 VUs, 5 minutes, target `GET /api/v1/lab/infrastructure/`.
3. Capture p50/p95/p99, error rate, PostgreSQL slow query log (>500 ms).
4. Repeat with `GET .../nodes/{id}/` for 20 random node IDs.

**Pass:** PERF-API-01/02/03 within targets; no N+1 regression vs 10-node baseline > 3× without documented cause (H-10).

---

### PERF-RUN-02 — Heartbeat burst (SAT-PERF-002)

**Preconditions:** Multi-agent lab or heartbeat simulator with valid tokens.

**Steps:**

1. Align all agents to heartbeat within same 10 s window.
2. Sustain 2× normal rate for 10 minutes.
3. Verify fleet statuses and `equipment_pcs` rollup for DSA agents.

**Pass:** PERF-HB-01/02; portal CPU < 80% sustained on staging sizing.

---

### PERF-RUN-03 — Concurrent sync

**Preconditions:** 5 EqPC with large raw files (≥100 MB each).

**Steps:**

1. Trigger sample accept on 5 bookings simultaneously.
2. Monitor DSA CPU, disk, SyncLog, portal ingest.

**Pass:** PERF-SYNC-01.

---

### PERF-RUN-04 — RA concurrent sessions

**Preconditions:** ≥5 Online Analysis PCs.

**Steps:**

1. Start RA sessions on 5 bookings within 1 minute.
2. Measure time-to-Guatemala connect; monitor tunnel table.

**Pass:** PERF-RA-01.

---

### PERF-RUN-05 — Scheduler load

**Preconditions:** RA-enabled equipment; synthetic users.

**Steps:**

1. Script 100 parallel booking POSTs for same slot window.
2. Observe allocation, queue, and DB locks.

**Pass:** PERF-SCH-01/02; document expected validation failures separately from server errors.

---

## 6. Tooling

| Tool | Use |
|------|-----|
| k6 / Locust | HTTP load for lab APIs |
| `django-debug-toolbar` / `SILKY` (staging only) | Query count per request |
| PostgreSQL `pg_stat_statements` | Slow query identification |
| pytest `SAT_PERF=1` | RA perf suite ([`docs/sat/README.md`](../sat/README.md)) |
| Portal metrics | Request latency, Celery queue depth |

---

## 7. Known limitations (performance)

| ID | Item | Impact |
|----|------|--------|
| H-10 | N+1 on large fleet tree/detail | p95 may exceed target at 100+ nodes until query optimization |
| — | Software compliance per-row exists() | Acceptable < 50 PCs; review at scale |
| — | SMS/WhatsApp alerts | Not load-tested (deferred channel) |

---

## 8. Entry / exit criteria

**Entry**

- [ ] Staging sized ≥ production vCPU/RAM ratio 0.5×
- [ ] `pg_stat_statements` enabled
- [ ] Baseline 10-node measurement captured for comparison

**Exit**

- [ ] PERF-API-02 PASS at 50 nodes **or** documented waiver with H-10 remediation plan
- [ ] PERF-HB-02 PASS on live agents
- [ ] No perf-related Critical defects open
- [ ] Baseline table in §4 filled and attached to Production Readiness Report

---

## 9. Sign-off

| Role | Name | Date | Notes |
|------|------|------|-------|
| Performance lead | | | |
| Portal engineering | | | |
| Ops | | | |
