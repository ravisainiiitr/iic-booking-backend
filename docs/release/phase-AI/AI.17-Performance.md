# AI.17 — Performance & Resource Isolation

**Date:** 2026-08-11

## Soft limits (code)

| Control | Default | Effect |
|---------|---------|--------|
| LLM timeout | 60s (Ollama) | Bounded worker wait |
| Max tokens | 800 | Bounded generation |
| Max input chars | 4000 | Bounded prompt |
| Max user messages / conversation | 40 | Bounded history |
| Max concurrent generations / process | 2 | Reject overload with busy message |
| User throttle | 60/hour | Copilot-only |
| Tool throttle | 30/hour | Copilot-only |

## Hard isolation design

- Inference **not** inside a long `@transaction.atomic` spanning Ollama
- Overload returns: *“Research Copilot is temporarily busy. Your booking and other portal operations are unaffected.”*
- Local Compose Ollama profile: `mem_limit` (default 4g) + `cpus` (default 2.0)
- Production: do not publish `11434` publicly; prefer private network

## Load / isolation test status

| Test | Status |
|------|--------|
| Unit concurrency busy path | Covered in `test_llm_provider_ai17.py` |
| Booking latency under Copilot load (prod) | **BLOCKED** until Ollama on EC2 + pilot credentials |
| Celery/DSA/RAA under Copilot load (prod) | **BLOCKED** (same) |

## Dev workstation (this session)

| Resource | Observed |
|----------|----------|
| Ollama binary | `0.32.9` installed |
| Models pulled | **none** at audit (`ollama list` empty) |
| Real inference E2E | **BLOCKED** until model pull succeeds |

Production EC2 CPU/RAM/GPU: **inspect before install** — see production probe section in implementation report.
