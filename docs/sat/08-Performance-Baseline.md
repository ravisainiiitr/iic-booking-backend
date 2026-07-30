# 08 — Performance Baseline

Record measured values on **staging** hardware. Placeholders must be replaced during SAT-08 (`SAT_PERF=1` / manual).

## Environment record

| Field | Value |
|-------|-------|
| Date | |
| Portal host / SKU | |
| DB | |
| Agent PC SKU / disk | |
| Network | |
| Portal SHA | |
| Agent version | |

## Baselines (fill during SAT)

| Metric | Target (initial) | Measured | Pass? |
|--------|------------------|----------|-------|
| Heartbeat p95 latency (1 agent) | < 500 ms portal processing | | |
| Heartbeat p95 under 20 agents @ 30s | < 2 s | | |
| 100 MB Input download (agent) | < 120 s on GigE | | |
| 1 GB Input download | < 20 min on GigE | | |
| 100 MB Output upload | < 120 s | | |
| 1 GB Output upload | < 20 min | | |
| 10 concurrent workspace prepares | all complete or fair-queue; no crash | | |
| Commissioning JSON poll | < 300 ms p95 | | |
| DB CPU during 20 HB | < 50% sustained | | |

Targets are **starting gates**; adjust with waiver if hardware differs — document actual SLO for production.

## Method notes

- Use identical sample payload SHA for repeated runs.
- Disable unrelated AV scans on lab path if they dominate (note in evidence).
- Do not run perf on production without change window.
