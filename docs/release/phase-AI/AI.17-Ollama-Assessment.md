# AI.17 — Ollama Assessment (pre-implementation)

**Date:** 2026-08-11  
**Baseline:** AI.16 deployed Copilot (`v2.5.5-ai16-research-copilot` / `7a3f552`); `RESEARCH_COPILOT_ENABLED=false`; OpenAI key unset.

## Classification matrix

| Area | Status | Notes |
|------|--------|-------|
| App structure | **IMPLEMENTED** | `research_copilot` models/views/tools/RAG/audit |
| Portal grounding | **IMPLEMENTED** | Server-side heuristic tools → `<<<PORTAL_DATA>>>` |
| Knowledge engine | **IMPLEMENTED** | Hybrid RAG; local embeddings default |
| Confirmation mutations | **IMPLEMENTED** | `requires_confirmation`; no LLM-authorized mutations |
| Prompt injection wrappers | **IMPLEMENTED** | `<<<UNTRUSTED_DOCUMENT_CONTEXT>>>` |
| Throttles / limits | **IMPLEMENTED** | 60/h chat, 30/h tools, timeout/max tokens |
| Pilot allowlist | **IMPLEMENTED** | `RESEARCH_COPILOT_PILOT_EMAILS` |
| LLM gateway ABC | **PARTIAL** | `LLMGateway` + `OpenAIGateway` + `FallbackGateway` only |
| Ollama provider | **MISSING** | Target of AI.17 |
| `COPILOT_LLM_PROVIDER` | **MISSING** | Selection currently = “has OPENAI_API_KEY?” |
| Provider health API | **MISSING** | Ops probes only |
| Docker Ollama service | **MISSING** | |
| Native OpenAI function calling | **MISSING** | Not required — tools are portal_grounding |
| Support `chat_ai` OpenAI | **DUPLICATE** | Separate from Copilot; leave unchanged |

## Current LLM call path

```
send_message → portal_grounding → rag.retrieve → prompt_builder
  → get_gateway() → OpenAIGateway | FallbackGateway → complete()
```

Extension point: **reuse `LLMGateway`**, add `OllamaGateway`, select via `COPILOT_LLM_PROVIDER`.

## Architectural rule for AI.17

Only add/replace the **LLM provider layer**. Do not redesign Copilot, tools, knowledge, booking, or Remote Analysis.
