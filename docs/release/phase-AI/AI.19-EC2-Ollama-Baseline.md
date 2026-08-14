# AI.19 — EC2 Ollama Baseline (BEFORE install)

**Captured:** 2026-08-14 19:03 UTC  
**Host:** `3.110.50.174` (`ip-10-0-1-153`)  
**DNS:** `equip.iitr.ac.in` still → `15.206.88.2` (unchanged; not a blocker for AI.19)

## Host

| Metric | Value |
|--------|-------|
| vCPU | 8 (AMD EPYC 7571) |
| MemTotal | 32221536 kB (~30.7 GiB) |
| MemAvailable | ~28.9 GiB |
| Swap | 0 |
| Disk `/` | 243G / 38G used / 16% |
| Load | 0.04, 0.07, 0.07 |
| GPU | none |

## Core health (localhost :8080)

| Endpoint | HTTP | Latency |
|----------|------|---------|
| `/api/version` | 200 | ~23 ms |
| analysis live | 200 | ~22 ms |
| analysis ready | 200 | ~64 ms (DB/cache/tunnel/guacamole ok) |

## Docker memory/CPU (pre-Ollama)

| Service | CPU | RAM |
|---------|-----|-----|
| django | 0.27% | ~479 MiB |
| celeryworker | 0.04% | ~472 MiB |
| celerybeat | 0.14% | ~154 MiB |
| flower | 0.01% | ~149 MiB |
| redis | 0.13% | ~24 MiB |
| reverse-tunnel-gateway | 0.00% | ~96 MiB |
| guacamole | 0.06% | ~496 MiB |
| guacd | 0.00% | ~16 MiB |
| guacamole-db | 0.01% | ~51 MiB |
| frontend | 0.00% | ~14 MiB |

## Copilot env (already present, OFF)

- `RESEARCH_COPILOT_ENABLED=false`
- `COPILOT_PROVIDER=ollama` / `COPILOT_LLM_PROVIDER=ollama`
- `OLLAMA_BASE_URL=http://127.0.0.1:11434` (will change to Docker service DNS)
- `OLLAMA_MODEL=llama3.2:3b` (will start with **1b** per AI.19)
- `RESEARCH_COPILOT_MAX_CONCURRENT=2` (will set to **1** initially)
- `RESEARCH_COPILOT_PILOT_EMAILS=` empty
- Port 11434: not listening; ollama binary absent

## Decision for install

Use **Docker `ollama/ollama`** on the backend compose network:

- **No public publish** of 11434
- CPU **2**, memory **8g**
- Model **llama3.2:1b**
- Django → `http://ollama:11434`
- Keep `RESEARCH_COPILOT_ENABLED=false` until qualification

---

## Post-install note

See [AI.19-Ollama-Production-Deployment.md](./AI.19-Ollama-Production-Deployment.md) for AFTER evidence. After AI.19 apply:

- `OLLAMA_BASE_URL=http://ollama:11434`
- `OLLAMA_MODEL=llama3.2:1b`
- `RESEARCH_COPILOT_MAX_CONCURRENT=1`
- flag remains **false**
