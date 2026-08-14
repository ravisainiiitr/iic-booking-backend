# EC2 Post-Upgrade Resource Baseline

**Captured:** 2026-08-14 ~18:56 UTC  
**Host:** `ubuntu@3.110.50.174` (`ip-10-0-1-153`)  
**Instance:** m5a.2xlarge (no GPU)  
**Context:** Phase 5A — DNS for `equip.iitr.ac.in` still pending (A → old IP).

## Host

| Metric | Value |
|--------|-------|
| vCPU (`nproc`) | 8 |
| Load average | 0.31, 0.16, 0.08 |
| Memory total | 30 GiB |
| Memory used | ~2.6 GiB |
| Memory available | ~27 GiB |
| Swap | none |
| Root filesystem | 243G total / 38G used / 205G free (**16%**) |
| GPU | unavailable |

## Docker (sample)

| Container | CPU % | Memory |
|-----------|-------|--------|
| django | 0.39% | ~441 MiB |
| celeryworker | 36.47%* | ~456 MiB |
| celerybeat | 2.89% | ~154 MiB |
| flower | 1.20% | ~149 MiB |
| redis | 0.81% | ~24 MiB |
| reverse-tunnel-gateway | 0.01% | ~96 MiB |
| guacamole | 0.07% | ~491 MiB |
| guacd | 0.00% | ~16 MiB |
| guacamole-db | 0.01% | ~51 MiB |
| frontend | 0.00% | ~14 MiB |

\*Celery CPU spike at sample time; re-check under booking load.

**Aggregate container RAM:** comfortably under ~3 GiB vs 30 GiB host.

## Ollama suitability (recommendation only — NOT installed)

| Item | Recommendation |
|------|----------------|
| First model | `llama3.2:1b` |
| Avoid initially | large 7B+ models on this shared host |
| CPU limit (future) | pin to ≤2–3 cores |
| Memory limit (future) | ≤4–6 GiB hard cap |
| Concurrency | `MAX_CONCURRENT=1` |
| Timeouts | strict request + inference timeouts |
| Network | bind Ollama to Docker network only; **never** publish `11434` to `0.0.0.0/0` |
| Copilot flag | keep `RESEARCH_COPILOT_ENABLED=false` until a dedicated pilot |

## Isolation reminder

Copilot/Ollama failure or CPU use must not starve booking, Celery, DSA sync, or RAA. Enforce cgroup limits before any install.

## DNS note

Public hostname smoke is **PENDING** until `equip.iitr.ac.in` → `3.110.50.174`.
