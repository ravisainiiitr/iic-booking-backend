# Performance Benchmark — Remote Analysis

**Date:** 2026-07-30  
**Environment:** Developer workstation, Django `local` settings, SQLite/local DB as configured, `mock_guacamole=True`  
**Tool:** `scripts/ra_phase3_benchmark.py`  
**Limitation:** Micro-benchmarks on a single machine — **not** production load / Guacamole RDP latency. Re-run on staging against PostgreSQL + Redis + live Guacamole before go-live.

---

## Micro-benchmark results (Phase 3)

| Operation | N | Avg (ms) | P95 (ms) | Max (ms) |
|-----------|---|----------|----------|----------|
| heartbeat_process | 40 | 4.87 | 6.89 | 30.39 |
| scheduler_refresh_health | 20 | 2.11 | 2.30 | 3.10 |
| reservation_create_cancel | 15 | 27.99 | 27.18 | 103.76 |
| scheduler_process_queue | 15 | 2.23 | 2.39 | 4.73 |
| scheduler_expire_stale | 15 | 3.68 | 3.94 | 3.98 |

Interpretation: portal-side scheduler/heartbeat paths are sub-10 ms typical on a warm local DB. Reservation create/cancel is higher (permissions, allocation, writes) with occasional ~100 ms spikes.

---

## Areas not timed in this pass (require staging)

| Area | How to measure | Target (pilot guidance) |
|------|----------------|-------------------------|
| Session startup (mock) | Wall time create→READY in UAT | < 5 s mock |
| Session startup (live Guacamole) | create→launch_url | < 15 s typical |
| Cleanup command round-trip | Portal queue → agent complete | < 60 s |
| Concurrent sessions (5) | 5 users launch | No allocation deadlock |
| Agent HTTP RTT | `portal_latency_ms` in heartbeats | < 500 ms campus LAN |
| DB query plans | `EXPLAIN` on reservation/session lists | Index use on status/time |

---

## Built-in performance telemetry

`operations.performance.PerformanceMonitor` aggregates:

- CPU / memory / disk from heartbeats  
- `portal_response_latency` / agent heartbeat latency snapshots  
- Session `launch_latency_ms`, `prepare_latency_ms`  
- Workspace sync throughput  

Exposed via Operations dashboard (`performance` section) and metric tables.

---

## Bottlenecks / risks

| Risk | Mitigation |
|------|------------|
| Reservation create under lock contention | Keep beat `process_reservation_queue` healthy; monitor queue length KPI |
| Large list endpoints without filters | Use `limit`/`offset` (max 200); avoid unbounded UI polls |
| Guacamole REST latency | Client timeout + one retry; size Guacamole/DB properly |
| Workspace large uploads | Gunicorn timeout; quotas; agent sync intervals |
| Dashboard rebuild | 60s snapshot cache |

---

## Concurrent sessions

Automated suite covers lifecycle scenarios; **five concurrent live RDP sessions** must be validated on pilot hardware (UAT). Expected: independent workstations, no shared Guacamole connection collisions (`max-connections=1` per connection object).

---

## Re-run instructions

```powershell
cd D:\IIC_NEW\iic-booking-backend
.\venv\Scripts\python.exe scripts\ra_phase3_benchmark.py
```

For staging: point `DJANGO_SETTINGS_MODULE` at production-like settings and disable mock only when Guacamole is up.

---

## Verdict

**PASS for portal control-plane latency** on local micro-bench.  
**WARNING:** Live Guacamole, cleanup, and 5-way concurrency require pilot staging measurements before declaring production performance complete.
