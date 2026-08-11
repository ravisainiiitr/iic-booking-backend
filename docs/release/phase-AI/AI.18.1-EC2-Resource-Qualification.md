# AI.18.1 — EC2 Resource Qualification (read-only)

**Date:** 2026-08-11  
**Probe run:** AI17 Production Host Resource Probe `31520526756` (post `v2.5.21` deploy)  
**Rule:** Complete **before** installing Ollama. No install performed in this phase.

## Host measurements

| Metric | Value | Notes |
|--------|-------|-------|
| Role | Self-hosted Linux EC2 runner co-located with production compose | Machine `ip-10-0-1-153` |
| MemTotal | **16232168 kB (~15.5 GiB)** | |
| MemAvailable | **~9979836 kB (~9.5 GiB)** | Snapshot after AI.18.1 deploy |
| MemFree | ~687856 kB | |
| SwapTotal | **0** | No swap — OOM risk if RAM exhausted |
| Root disk | **49G total, 35G used, 14G avail (72%)** | Model pulls compete with images/logs |
| GPU | **`nvidia-smi_absent`** | CPU-only inference only |
| Ollama container | **Absent** | `no_ollama_container` |
| Ollama process | **Absent** | `no_ollama_process` |
| `nproc` | **4** | Confirmed `nproc=4` on re-probe `31521083962` |
| `uname` | Linux ip-10-0-1-153 6.8.0-1047-aws … x86_64 | |
| `loadavg` | `0.65 0.80 1.05` | Light at probe time |


## Docker memory snapshot (post-deploy)

| Container | CPU % (snap) | Mem |
|-----------|--------------|-----|
| django | 34.43% | 639.8 MiB |
| celeryworker | 0.32% | 481.9 MiB |
| guacamole | 0.16% | **1.468 GiB** |
| celerybeat | 0.00% | 168.4 MiB |
| flower | 0.02% | 172.6 MiB |
| reverse-tunnel-gateway | 3.87% | 59.5 MiB |
| redis / frontend / guacd / guac-db | low | <60 MiB each |

Approximate **steady production footprint ≈ 3+ GiB** before any LLM runtime. Guacamole alone holds ~1.5 GiB.

## Suitability analysis

| Criterion | Assessment |
|-----------|------------|
| Spare RAM for small CPU model (1b–3b) | Numerically ~9.5 GiB available — **looks enough on paper** |
| No swap | **Hostile** to co-resident LLM spikes |
| Disk headroom | **Tight** (72% used; 14G free) for base image + model layers |
| GPU | None — latency/CPU contention with Django (observed 34–81% CPU on django in probes) |
| Blast radius | Same host as booking portal, Guacamole, Celery, tunnel gateway |

## Ollama decision

### **BLOCKED** (co-resident install on this production EC2)

Do **not** install Ollama on `ip-10-0-1-153` until an operator-approved plan provides **at least one** of:

1. **Dedicated AI host** (preferred) with private network path to portal, or  
2. Explicit capacity reservation: add swap or raise instance size, cap Ollama (`cpus`/`memory` in compose), bind **127.0.0.1:11434 only**, use ≤`llama3.2:1b`, `RESEARCH_COPILOT_MAX_CONCURRENT=1`, and a documented rollback.

Phases 9–13 (private Ollama architecture/limits prep + failure isolation) are **deferred** to **AI.18.2** under that plan. Copilot flag remains **false**.

## Copilot flag (this phase)

| Setting | Required | Observed |
|---------|----------|----------|
| `RESEARCH_COPILOT_ENABLED` | false | **false** (`PASS` after migrate `31520513861`) |
| Pilot emails | empty | empty (asserted in migrate/security probes) |
| OpenAI key for prod path | not required | not used for enablement |
