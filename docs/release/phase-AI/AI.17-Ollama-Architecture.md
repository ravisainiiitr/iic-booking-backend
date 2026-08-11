# AI.17 — Ollama Architecture

**Date:** 2026-08-11

## Principle

Research Copilot is an **additive** intelligence layer. Booking, DSA, RAA, Celery, wallet, payments, and notifications must continue if Ollama is powered off.

```
User → Portal API (/api/v1/research-copilot/)
         → Feature gate (RESEARCH_COPILOT_ENABLED + PILOT_EMAILS)
         → Conversation service
              → Portal grounding (server-side allowlisted tools)
              → Knowledge RAG
              → Inference concurrency gate
              → Inference Provider (COPILOT_PROVIDER)
                   → OllamaGateway  (default, production path)
                   → OpenAIGateway  (optional)
                   → FallbackGateway / FakeInferenceProvider (tests)
```

## Provider surface

| Method | Purpose |
|--------|---------|
| `generate()` / `complete()` | Non-streaming completion |
| `stream()` | Optional streaming (bounded) |
| `health()` | Reachability + model presence |
| `model_available()` | Boolean convenience |

## Configuration

| Env | Default | Notes |
|-----|---------|-------|
| `COPILOT_PROVIDER` | `ollama` | Preferred selector (`COPILOT_LLM_PROVIDER` still accepted) |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Private only — never public |
| `OLLAMA_MODEL` | `llama3.2:3b` | Override without code change |
| `RESEARCH_COPILOT_LLM_TIMEOUT_SECONDS` | `60` | Generation timeout |
| `RESEARCH_COPILOT_MAX_TOKENS` | `800` | Output cap |
| `RESEARCH_COPILOT_MAX_INPUT_CHARS` | `4000` | Input cap |
| `RESEARCH_COPILOT_MAX_CONCURRENT` | `2` | Per-process generation slots |
| `RESEARCH_COPILOT_ENABLED` | `false` | Authoritative enable |
| `RESEARCH_COPILOT_PILOT_EMAILS` | empty | Allowlist when enabled |

`OPENAI_API_KEY` is **not** required when `COPILOT_PROVIDER=ollama`.

## Isolation

1. LLM calls are **outside** long DB transactions.
2. Concurrency gate rejects overload with a busy message (no unbounded queue).
3. Copilot throttles (`60/hour`, `30/hour` tools) are separate from portal traffic.
4. Ollama Docker (local profile) uses `mem_limit` / `cpus` caps.
5. Tool mutations remain confirmation-based via existing portal services.

## Failure modes

| Condition | Copilot | Portal |
|-----------|---------|--------|
| Ollama offline | Unavailable message | Unaffected |
| Model missing | Unavailable | Unaffected |
| Timeout | Unavailable / escalate | Unaffected |
| Concurrent saturation | Busy message | Unaffected |
| Flag false | Disabled (503/bootstrap enabled=false) | Unaffected |
