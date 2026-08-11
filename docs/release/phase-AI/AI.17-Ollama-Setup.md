# Ollama local setup (AI.17)

Ollama runs as a **separate** service. Django never embeds model weights.

## Local (Docker Compose)

```bash
docker compose -f docker-compose.local.yml --profile ollama up -d ollama
docker compose -f docker-compose.local.yml --profile ollama exec ollama ollama pull llama3.2:3b
docker compose -f docker-compose.local.yml --profile ollama exec ollama ollama list
```

Django (in compose) uses:

```
COPILOT_LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2:3b
```

Host Django (not in Docker) uses:

```
OLLAMA_BASE_URL=http://127.0.0.1:11434
```

Optional GPU: uncomment the `deploy.resources.reservations.devices` block under `ollama` in `docker-compose.local.yml` when NVIDIA Container Toolkit is installed. CPU-only remains supported.

## Local (native Windows / Linux)

1. Install Ollama (e.g. `winget install Ollama.Ollama` or https://ollama.com).
2. `ollama pull llama3.2:3b`
3. `ollama list`
4. Set env as above with `OLLAMA_BASE_URL=http://127.0.0.1:11434`.

## Model guidance (evidence-based)

| Host class | Suggested default | Notes |
|------------|-------------------|-------|
| Dev laptop ≥16 GB RAM + optional GPU | `llama3.2:3b` | Fast tool-grounded replies; low VRAM |
| Dev workstation ≥32 GB / 16+ GB VRAM | `llama3.1:8b` or `qwen2.5:7b` | Better quality; measure latency |
| Small production app host (shared Django/Postgres/Redis) | **Do not co-locate large models** | Prefer separate private AI host |

Do **not** auto-pull models on every deploy.

## Production networking

- Bind Ollama to private network only.
- **Never** publish `11434` to the public Internet.
- Only the Django/backend service may call Ollama.

## Rollback

`RESEARCH_COPILOT_ENABLED=false` disables Copilot. Core portal stays up if Ollama stops.
