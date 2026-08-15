# AI.25.2 — Root Cause Analysis

**Date (UTC):** 2026-08-15  
**Context:** AI.25.1 authenticated 86-query regression failed (timeout 38.4%, avg ~41.8s) on deployed AI.24.1 with PUBLIC OFF.

## Safety freeze (verified throughout)

```text
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PUBLIC_ENABLED=false
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
RESEARCH_COPILOT_MAX_TOKENS=160
RESEARCH_COPILOT_MAX_CONCURRENT=1
OLLAMA_MODEL=llama3.2:1b (2 CPU / 8 GB)
```

Public Copilot remained **OFF**. Pilot not expanded. No 3B. No concurrency increase.

## What is *not* the cause

| Hypothesis | Evidence |
|------------|----------|
| Authorization / ACL regression | Safe 100%, hall 0%, security probes green in AI.25.1 |
| Public-mode code path | PUBLIC=false; authenticated pilot only |
| Prompt bloat from AI.24.1 | **Identical `prompt_chars`** for matched IDs vs AI.23 (e.g. Q-A-001=2336, Q-S-001=2080) while latency doubled |
| `max_tokens` not reaching Ollama | Gateway sends `max_tokens` + `options.num_predict=160` (AI.21.2) |
| History unbounded | Still 4×450 in `prompt_builder` |
| Django/tool DB latency | `llm_latency_ms ≈ wall_ms`; portal tools ms-level |

## Measured bottleneck

Direct Ollama `/api/chat` on production (2 CPU limit):

| Case | Wall | Prompt eval | Gen |
|------|------|-------------|-----|
| Short warm | ~5–7s | cached ~90ms | ~9 tok/s |
| Long cold (~441 tok) | ~24s | ~22.7s @ ~19 tok/s | ~9 tok/s |

Implications for ~600–700 prompt tokens + up to 160 completion tokens:

- Cold prompt eval alone can consume **30–40s**
- Generation at ~9 tok/s adds **10–18s**
- Sum frequently approaches / exceeds the **60s** provider timeout

During AI.25.1 soak, Ollama CPU sat at ~200% of the 2-CPU envelope. Scorecard timeouts were almost all `llm_ms≈60000`.

## Why AI.24.1 felt worse than AI.23 despite same prompt sizes

1. **Inference dominates** — same-sized prompts still sit near the 60s cliff on current Ollama 0.32.11 / 2-CPU envelope.
2. **AI.23 survival depended on not spending the full budget** — many portal-authoritative queries still went through Ollama narration; when the host was slightly faster or KV warmer, they finished under 58s. AI.25.1’s sequential soak (and early dual-eval contention) pushed more turns over the cliff.
3. **AI.24.1 itself did not enlarge prompts** for matched definitional rows — the regression is **not** “ACL made prompts bigger,” it is **“CPU Ollama cannot reliably finish 2k-char system prompts + 160 tokens within 60s under load.”**
4. **Missing deterministic coverage** after AI.22.2 — only mixed cost+prepare and equipment-list had skip-LLM paths. Status/pricing/docs/software still paid full Ollama cost.

## Instrumentation used

Existing metadata: `portal_grounding_ms`, `rag_ms`, `rag_skipped`, `prompt_chars`, `llm_latency_ms`, `llm_error_category`, token counts.

AI.25.2 added: `llm_wall_ms`, `total_ms` on LLM path.

## Root cause (one sentence)

**Authenticated latency regression is Ollama CPU generation time against the frozen 1b/2CPU/60s envelope; AI.24.1 ACL is not the primary cause. Recovery requires skipping Ollama when portal tools already hold the authoritative answer (extend AI.21.2/AI.22.2 deterministic routing), not raising resources first.**
