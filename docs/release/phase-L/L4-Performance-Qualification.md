# Phase L4 — Performance Qualification Report

**Date:** 2026-08-06  
**Portal:** https://equip.iitr.ac.in  
**Backend:** `v2.5.0-rc24-release`

## Measurements (client-side wall time)

| Probe | HTTP | Latency |
|-------|------|---------|
| GET `/api/equipments/` (cold) | 200 | ~8076 ms |
| GET `/api/equipments/1/slots/` | 200 | ~1364 ms |
| GET `/api/bookings/?list_view=1` | 200 | ~527 ms |
| GET `/api/wallet/` | 200 | ~354 ms |
| GET `/api/v1/analysis/workstations/` | 200 | ~361 ms |
| DSA local `/api/health` | 200 | ~51 ms |
| RA local `/health` | 200 | ~56 ms |
| 8× concurrent GET equipment detail | 200 | 422–1459 ms |

## Host resources (EC2)

| Resource | Observation |
|----------|-------------|
| CPU (containers) | Idle &lt;1% Django/Celery; Guacamole ~1.3 GiB RAM |
| Memory | 15 GiB host; ~5.1 GiB used; ~9.7 GiB available |
| Load | ~1.0–1.6 (158-day uptime) |
| Disk `/` | **80% used** (39G/49G) — monitor |
| Redis | AOF on; RDB last save OK; ~8 MiB used |
| Celery | `inspect ping` → pong (1 node) |

## Prior Phase L evidence reused

- Concurrent DSA sync: N/A (single live agent) — L1  
- Large uploads 100 MB / 500 MB resume / 1 GB: PASS — L1  
- Concurrent RA sessions: N/A (single Analysis PC) — L2  

## Bottlenecks / recommendations

1. Cold `/api/equipments/` catalog can exceed 5 s — warm caches / pagination review post go-live.  
2. Root disk at 80% — schedule log/image prune and confirm DB backup off-box.  
3. Guacamole memory footprint is the largest single container — size host accordingly for concurrent RDP.

## Verdict

**L4 PASS** for pilot/institute rollout scale with monitoring on disk and catalog latency.
