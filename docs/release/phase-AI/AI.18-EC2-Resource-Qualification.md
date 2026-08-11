# AI.18 — EC2 Resource Qualification (read-only)

**Date:** 2026-08-11  
**Rule:** Complete **before** installing Ollama on production.

## Sources

| Probe | Run | Result |
|-------|-----|--------|
| AI11 Observability | `31516455870` | success |
| AI16 Git pointer | `31516368237` | success |
| AI17 Host Resource Probe workflow | On master via AI.18 merge | Runnable after deploy (was missing from default branch previously) |

## Observed (AI11 / AI16)

| Item | Value |
|------|-------|
| Host role | Self-hosted Linux EC2 runner co-located with production compose |
| Django | `iic-booking-backend-django-1` Up (healthy) |
| Ready | HTTP 200 |
| Ollama container/process | **Not observed** in AI11 `docker ps` sample |
| GPU | **Not measured** in AI11 (no nvidia-smi step) |
| CPU cores / load / MemTotal | **Not measured** in AI11 — **BLOCKED until AI17 host probe runs successfully** |

## Guidance (pending measured numbers)

Do **not** install Ollama on this host until:

1. `nproc`, `/proc/meminfo`, `df -h /`, and optional `nvidia-smi` are recorded
2. Docker stats show spare headroom for Django/Postgres/Redis/Celery
3. Limits chosen: prefer separate AI host; if co-located, use small model + `RESEARCH_COPILOT_MAX_CONCURRENT=1–2` + cgroup mem/cpu caps
4. Bind Ollama to **private** interface only — never `0.0.0.0:11434` public

## Decision gate

| Gate | Status |
|------|--------|
| Measured EC2 CPU/RAM/disk/GPU | **BLOCKED / PENDING** host probe execution post-merge |
| Ollama install approved | **NO** — resources not yet qualified |
| Copilot enablement | **NO** — independent of Ollama install; flag stays false |


## Post-attempt production pointer (AI16 31518833261)

- current_release_tag=`v2.5.5-r11-catalog-sync.2`
- previous_release_tag=`v2.5.20-ai18-research-copilot-off`
- Django healthy after concurrent catalog deploy

