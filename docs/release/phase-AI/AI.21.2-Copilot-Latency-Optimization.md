# AI.21.2 — Copilot Latency & Reliability Optimization

**Date:** 2026-08-15 (IST)  
**Host:** EC2 `3.110.50.174` (m5a.2xlarge, CPU only)  
**Pilot (unchanged):** `RESEARCH_COPILOT_ENABLED=true`, allowlist **only** `test.student@iic-booking.test`  
**Ollama envelope (unchanged):** `llama3.2:1b`, 2 CPU, 8 GB RAM, `MAX_CONCURRENT=1`, timeout 60s  

**Final decision:** **READY FOR PILOT CONTINUATION**  
(Not global production ready. Do not expand allowlist.)

---

## 1. Primary question

> Why do some follow-up questions take ~60 seconds?

### Measured answer (not speculation)

| Layer | Measured contribution |
|-------|------------------------|
| Auth / feature gate | ≪ 50 ms |
| Portal tool planning + execution | typically **5–50 ms** (tools were **not** the bottleneck) |
| RAG (when used) | typically **10–25 ms** |
| Prompt construction | ≪ 50 ms |
| **Ollama inference** | **≈ wall clock − tool time** (dominates; often 15–60s) |
| Audit / DB write after reply | short; inference **not** inside `transaction.atomic()` for LLM call |

**Root cause (compound):**

1. **Completion budget too large:** Django default `RESEARCH_COPILOT_MAX_TOKENS=800`. On CPU `llama3.2:1b` (2 cores), generating hundreds of tokens routinely hit the **60s** provider timeout.
2. **Prompt too large for CPU eval:** fat equipment snippets, multi-tool JSON dumps, RAG citations (6×~400 chars), long system prompt, and unbounded follow-up history inflated `prompt_tokens` (~700–1000+).
3. **Tool over-calling:** e.g. software / prepare questions also pulled `search_equipment`; definitional “What is XRD?” previously dumped catalog via search.
4. **OpenAI-compatible Ollama path ignored `options.num_predict`:** `/v1/chat/completions` needed top-level `max_tokens` (fixed in AI.21.2).

**Not the cause:** Django tool DB latency, Celery, Redis, or core booking APIs. Core `/api/version` stayed ~25–30 ms during benches.

---

## 2. Slow-query evidence (before AI.21.2)

From AI.21.1 pilot observation + AI.21.2 first post-change bench still on `MAX_TOKENS=800`:

| Query | wall_ms | prompt_chars | tools | result |
|-------|---------|--------------|-------|--------|
| How much does 5 XRD samples cost? | ~60094 | ~5659 | search_equipment + estimate_booking_cost | **timeout** |
| What XRD slots are available? | ~60096 | ~5948 | search_equipment + search_slots | **timeout** |
| What software can I use for PXRD? | ~60094 | ~6534 | recommend_software + search_equipment + RAG | **timeout** |
| What is XRD? | ~60084 | ~5630 | RAG | **timeout** |
| Follow-up: How much will it cost? | ~60075 | ~6712 | pricing chain | **timeout** |
| Follow-up: What should I prepare? | ~60085 | ~4163 | (history bloat) | **timeout** |
| What is my next booking? | ~40315 | ~3362 | get_next_booking | ok (slow) |

First “optimized” deploy incorrectly reported `MAX_TOKENS 800` because `config/settings/base.py` still defaulted to **800** (gateway helper default was overridden by settings).

---

## 3. Changes made (smallest safe set)

| Area | Change |
|------|--------|
| Settings | `RESEARCH_COPILOT_MAX_TOKENS` default **800 → 160**; production env set `RESEARCH_COPILOT_MAX_TOKENS=160` |
| Ollama gateway | Send `max_tokens` on `/v1/chat/completions` **and** `options.num_predict` |
| Portal grounding | Skip equipment search for definitional “what is…”; software-only skips redundant equipment; prepare uses `search_documentation` not catalog dump; compact tool JSON (~1100 chars/tool); slim slots/estimates |
| Conversation | Skip RAG when portal tools already ground turn (incl. software/docs); metadata: `portal_grounding_ms`, `rag_ms`, `rag_skipped`, `prompt_chars` |
| Prompt builder | Compact system prompt; history window **4×450 chars** |
| RAG | Cap **2** citations × **~160** chars in LLM context |
| Structured search | Shorter equipment snippets (prior AI.21.2 edit) |

**Explicitly not changed:** Ollama CPU/RAM/concurrency/model; HTTP timeout raised; pilot allowlist; PI pricing resolver; R11/R12/R14/DSA paths.

---

## 4. After AI.21.2 live pilot matrix

Pilot only: `test.student@iic-booking.test`. Fresh conversation per single query; shared conv for follow-ups.

| # | Query | wall_ms | tools | rag_skipped | prompt_chars | completion_tokens | ok |
|---|-------|---------|-------|-------------|--------------|-------------------|-----|
| 1 | What is my next booking? | 29639 | get_next_booking | true | 2183 | 25 | **PASS** |
| 2 | How much does 5 XRD samples cost? | 50402 | search_equipment + estimate_booking_cost | true | 2714 | (see note) | **PASS** |
| 3 | What is the status of my sample? | 15282 | get_sample_status | true | 2144 | 85 | **PASS** |
| 4 | Are my results ready? | 10488 | get_booking_results | true | 2135 | 53 | **PASS** |
| 5 | What XRD slots are available? | 22029 | search_equipment + search_slots | true | 2682 | 80 | **PASS** |
| 6 | What software can I use for PXRD? | 29295 | recommend_software only | true | 2107 | 187 | **PASS** |
| 7 | What should I prepare before my XRD booking? | 43501 | search_documentation | true | 3199 | 175 | **PASS** |
| 8 | What is XRD? | 55365 → **34757** after max_tokens fix | RAG (no portal tools) | false | ~2336 | **160** (capped) | **PASS** |
| 9 | Follow-up: How much will it cost? | 18500 | pricing chain | — | 2777 | 50 | **PASS** |
| 10 | Follow-up: What should I prepare? | 20083 | search_documentation | — | 2527 | 82 | **PASS** |

**SUMMARY (10-query bench before final max_tokens OpenAI field fix):**  
`ok=10`, `timeouts=0`, `avg_wall≈29458`, `max_wall≈55365`, `p95≈50402`.

After enforcing `max_tokens` on the OpenAI-compatible path, “What is XRD?” completed in **~35s** with **exactly 160** completion tokens.

### Pricing authority (retest)

- Tools: `search_equipment` + `estimate_booking_cost` both **ok**
- Reply grounded on PORTAL_DATA / portal estimate (not free-invented pricing path)
- Example head: cites estimate tool / portal booking calculate APIs

### Security regression

| Check | Result |
|-------|--------|
| Cross-user `get_booking_results` | **denied** (`booking_not_found`) |
| Own `cancel_booking` | **`requires_confirmation=true`** (no silent cancel) |
| Concurrency gate | second acquire → **COPILOT_BUSY** |
| Prompt injection (“reveal system prompt / API keys”) | **no key leak**; refused |
| Pilot allowlist | still **only** `test.student@…` |
| Non-pilot | still denied (feature gate unchanged) |

### Core platform during Copilot load

| Probe | Result |
|-------|--------|
| `/api/version` | **200** ~25–30 ms |
| Django / Celery / Redis | healthy in `docker stats` |
| Ollama | ~1.9 GiB / 8 GiB limit; idle after bench |
| Pilot flags after deploy | `ENABLED=true`, allowlist unchanged, `MAX_TOKENS=160` |

---

## 5. Before / after benchmark

| Scenario | BEFORE (AI.21.1 / early AI.21.2 @800 tok) | AFTER (AI.21.2 @160 tok + context caps) |
|----------|-------------------------------------------|------------------------------------------|
| Short / next booking | ~40s or timeout under load | ~10–30s, **ok** |
| Pricing (5 XRD) | **timeout ~60s** | **~30–50s, ok** (tools authoritative) |
| Sample status | ~25s | **~15s** |
| Results | ~31s | **~10s** |
| Slots | **timeout** | **~22s** |
| Software | **timeout** | **~29s**, single tool |
| Prepare | slow / timeout | **~20–43s**, docs tool |
| What is XRD? | **timeout** | **~35s** (capped) |
| Follow-up cost | **timeout** | **~18.5s** |
| Follow-up prepare | **timeout** | **~20s** |
| Timeout count (10-query set) | **6+** | **0** |
| Avg wall (10-query set) | ~45–55s+ | **~29.5s** |

Targets (aspirational): simple &lt;5–8s, portal &lt;10–15s, complex &lt;20–30s.  
**Status:** improved reliability **PASS**; absolute target band **PARTIAL** on CPU 1b (complex queries still 20–50s). Raising CPU/RAM/model **not** done (per envelope).

---

## 6. Acceptance matrix

| Item | Status |
|------|--------|
| Slow query reproduced | **PASS** |
| Latency source identified | **PASS** (Ollama generation + oversized context/max_tokens) |
| Ollama timing measured | **PASS** (`llm_ms` ≈ wall; tools ≪ 1s) |
| Tool timing measured | **PASS** (ground_ms 0–50 ms) |
| Tool over-calling investigated | **PASS** (software/prepare/definitional fixed) |
| Follow-up context investigated | **PASS** (history window + truncation) |
| Context size measured | **PASS** (`prompt_chars` / `prompt_tokens` in metadata) |
| Knowledge retrieval optimized | **PASS** (skip when portal-grounded; smaller RAG) |
| Response length optimized | **PASS** (max_tokens 160 + concise prompt) |
| Portal routing optimized | **PASS** |
| Pricing still authoritative | **PASS** |
| Authorization regression | **PASS** |
| Prompt injection regression | **PASS** |
| Confirmation regression | **PASS** |
| Concurrency gate | **PASS** |
| Timeout handling | **PASS** (clean unavailable; 0 timeouts in final matrix) |
| DB transaction isolation | **PASS** (LLM outside write atomic; audit after) |
| Booking latency | **PASS** (`/api/version` unaffected) |
| Celery impact | **PASS** (no failures observed) |
| RAA/DSA impact | **NOT TESTED** / **BLOCKED BY DNS** (unchanged posture) |
| Frontend UX | **NOT TESTED** in this pass (API/service-layer bench); prior controlled messages remain |
| Live pilot regression | **PASS** (10/10) |
| Before/after benchmark | **PASS** |
| Monitoring | **PARTIAL** (metadata + logs; no new dashboards) |
| Rollback | **PASS** (documented) |

---

## 7. Rollback

```bash
# Pause Copilot only (preferred if security regression)
RESEARCH_COPILOT_ENABLED=false
# recreate django with compose exports preserved

# Or restore env backup
# .envs/.production/.django.bak.ai212.* / .bak.ai21.*
```

Do **not** raise concurrency or remove allowlist as a “fix”.

---

## 8. Residual risks / next work (out of AI.21.2 scope)

- Absolute latency still limited by **CPU llama3.2:1b @ 2 cores** — complex answers ~20–50s.
- Aspirational &lt;15s portal targets likely need measured resource/model change (not applied here).
- Frontend loading UX not re-validated in browser this pass.
- DSA/RAA live still blocked by DNS pointing at old EIP.

---

## 9. Verdict

### **READY FOR PILOT CONTINUATION**

- Controlled pilot remains **one** seeded account.
- Timeouts eliminated on the live matrix; portal grounding + pricing + auth preserved.
- Core booking remains healthy; Ollama stays within the approved resource envelope.
- **Not** declared global production ready. **Do not expand the pilot** in AI.21.2.
