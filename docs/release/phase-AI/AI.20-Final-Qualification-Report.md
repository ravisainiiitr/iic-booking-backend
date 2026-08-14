# AI.20 — Final Qualification Report

**Verdict: PARTIAL — PILOT BLOCKED**

Research Copilot is **functionally closer to a limited pilot**, with Ollama remaining on the AI.19 safe envelope. Production stays **`RESEARCH_COPILOT_ENABLED=false`** because no authorized pilot emails were supplied.

DNS / DSA / RAA live coexistence remain **BLOCKED BY DNS** and do **not** fail AI.20 Copilot-specific gates.

---

## Final acceptance matrix

| Item | Status | Evidence |
|------|--------|----------|
| Authentication | **PARTIAL** | Unauth API 401; service bootstrap/gate PASS; live password login NOT TESTED (no invented creds) |
| Feature flag | **PASS** | Prod `false`; bootstrap `enabled=false`; 503 gate |
| Pilot allowlist | **PASS** (logic) / **BLOCKED** (enablement) | In-process override PASS; prod allowlist empty |
| Portal grounding | **PASS** | `run_portal_grounding` + PORTAL_DATA blocks; XRD pricing chain |
| Booking tools | **PASS** | `get_next_booking` / `search_bookings` |
| Slot search | **PASS** | XRD→equipment 40; empty slots noted without invention |
| Wallet | **PASS** | Own wallet tool; injection chat did not leak other user |
| Sample status | **PASS** | Planner + tool; missing booking → explicit error |
| Sample deadline | **PASS** | Planned/executed; missing → explicit error |
| Results | **PASS** | Own-scope; cross-user denied |
| Software | **PASS** | `recommend_software` returns catalog rows |
| Pricing | **PASS** | `estimate_booking_cost` ok; `estimate=null` + portal calculate CTA — **no hallucinated price** |
| PI pricing | **NOT TESTED** | Needs real PI/wallet fixtures + pilot enable |
| Knowledge base | **PARTIAL** | RAG/citations path present; dedicated KB accuracy set NOT fully scored |
| General knowledge | **PARTIAL** | Answers returned; 1b quality uneven (FWHM) |
| Mixed questions | **PARTIAL** | Grounding ran; reply quality mixed under 1b/timeouts |
| Authorization | **PASS** | Foreign booking `#405` denied for user A |
| Mutating actions | **PASS** | Cancel prepare only |
| Review & Confirm | **PASS** | `requires_confirmation=true` |
| Prompt injection | **PASS** | UNTRUSTED wrapper + chat refused wallet leak |
| Tool failure | **PASS** | Explicit `ok=false` / missing ids; no fabricate |
| Ollama failure | **PASS** | Controlled unavailable; core healthy |
| Timeout | **PASS** | 60s TimeoutError → controlled unavailable |
| Concurrency | **PASS** | Busy response at max=1 |
| DB transaction isolation | **PASS** | `in_atomic_block=False` during generate |
| Booking latency | **PARTIAL** | version ~23ms / ready ~62–75ms during inference |
| Celery impact | **PARTIAL** | ping OK; worker CPU low during Ollama peak |
| Result processing | **NOT TESTED** | |
| Frontend | **PARTIAL** | Build PASS; live chat UI NOT TESTED (flag OFF) |
| Android | **PARTIAL** | test+assembleDebug PASS; no Ollama direct; live chat NOT TESTED |
| Audit | **PASS** | CopilotAuditEvent rows (busy/tool/escalate/disabled) |
| Rate limiting | **PASS** (config) | `60/hour` chat, `30/hour` tools deployed |
| Monitoring | **PARTIAL** | docker stats + gateway health/latency/busy |
| Resource isolation | **PASS** | AI.19 caps retained; Copilot degrades first |
| Rollback | **PASS** | Flag false verified end-to-end |
| Controlled pilot | **BLOCKED** | Empty `RESEARCH_COPILOT_PILOT_EMAILS` |

---

## Preferred successful state vs actual

| Target | Actual |
|--------|--------|
| READY FOR LIMITED PRODUCTION PILOT | **Not met** — allowlist empty |
| allowlist only | N/A (flag OFF) |
| Global users disabled | **Yes** |
| llama3.2:1b / 2 CPU / 8 GB / conc=1 | **Yes** |
| DNS pending separately | **Yes** |
| DSA/RAA pending DNS | **Yes** |

---

## Rollback

```bash
# Already active:
RESEARCH_COPILOT_ENABLED=false

# If resource pressure:
docker stop iic-booking-backend-ollama-1
```

---

## Next operator actions

1. Supply real pilot emails → set `RESEARCH_COPILOT_PILOT_EMAILS`
2. Ship AI.20 Copilot code fixes in a normal image deploy
3. Only then set `RESEARCH_COPILOT_ENABLED=true` (allowlist required)
4. Re-qualify HTTP pilot vs non-pilot + pricing/PI matrix
5. Keep model at **1b** until prompt/KB quality work is exhausted
