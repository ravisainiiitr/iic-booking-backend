# AI.22 — Research Copilot Intelligence, Query Coverage & Answer Quality

**Date:** 2026-08-15 (IST)  
**Host:** EC2 `3.110.50.174`  
**Pilot (unchanged):** `test.student@iic-booking.test` only  
**Envelope (unchanged):** `llama3.2:1b`, 2 CPU, 8 GB, concurrent 1, `MAX_TOKENS=160`

**Final decision:** **READY FOR PILOT CONTINUATION — IMPROVEMENTS REQUIRED**  
(Not ready for pilot expansion. Not global.)

Companion dataset: [AI.22-Copilot-Evaluation-Dataset.md](./AI.22-Copilot-Evaluation-Dataset.md)

---

## 1. Primary question

> How do we make Research Copilot answer the maximum possible number of real IIC Booking questions correctly, safely, and with minimal human intervention?

AI.22 answer (measured):

1. Keep **portal-first** tool routing.
2. Fix **authoritative pricing** (was returning `estimate: null` → model could invent amounts).
3. Add **deterministic clarification** for underspecified questions (success without LLM).
4. Add **follow-up enrichment** from recent user turns (compact; preserves AI.21.2 context caps).
5. Expand phrase coverage (results / deadlines / remote analysis).
6. Do **not** expand model/CPU/RAM/concurrency or the pilot allowlist.

---

## 2. Capability inventory (existing)

| Capability | Tool / path | Source of truth | AuthZ |
|------------|-------------|-----------------|-------|
| Chat / stream | conversation API | — | pilot allowlist + auth |
| Next booking | `get_next_booking` | Booking | own user |
| Bookings list | `search_bookings` | Booking | own user |
| Wallet | `get_wallet` | Wallet | own / accessible |
| Sample status | `get_sample_status` | sample_trace | own booking |
| Sample deadline | `get_sample_deadline` | deadline service | own booking |
| Results | `get_booking_results` | result merge | own booking; no public URLs |
| Slots | `search_slots` | DailySlot | catalog |
| Equipment | `search_equipment` | Equipment | visibility |
| Software | `recommend_software` | R6 catalog | catalog |
| Docs | `search_documentation` / RAG | Knowledge* | security levels |
| Pricing | `estimate_booking_cost` | **ChargeCalculationEngine** (AI.22 wired) | user charge profile |
| Mutations | create/cancel/launch/ticket | portal href cards | confirmation required |
| Concurrency | `acquire_generation_slot` | — | busy reject |
| Injection | prompt wrappers | — | untrusted docs |

No second Copilot engine was created.

---

## 3. Pilot query analysis (anonymized)

Source: production Copilot messages for allowlisted pilot (`pilot-student`).

| Metric | Value |
|--------|-------|
| Recent user messages observed | 39 (mostly AI.21.x bench repeats) |
| Distinct intents in SearchQueryLog (sample) | equipment, general, policy, status |
| Categories exercised | booking, pricing, sample, results, slots, software, prep, science, injection |

**Quality note (pre-AI.22):** HTTP 200 ≠ correct. Pricing tool returned `estimate: null`; narrative replies could invent figures (e.g. “$0.72”) — classified as **HALLUCINATION risk** even when tools “ok”.

---

## 4. Coverage gaps found

| Gap | Class | Fix in AI.22? |
|-----|-------|----------------|
| `estimate_booking_cost` always `null` | tool insufficient | **Yes** — call `_calculate_one_proforma_line` + field defaults + `num_samples` |
| Ambiguous “Can I book it?” | clarification missing | **Yes** — deterministic clarification |
| Follow-up “How much will it cost?” without entity | context | **Yes** — enrich from prior user text |
| “Are my results ready?” / download phrasing | routing | **Yes** — expanded phrases |
| “When should I submit my sample?” | routing | **Yes** — deadline phrases |
| Remote analysis questions | routing | **Yes** — map to `recommend_software` |
| XRD family resolves to GI-XRD (id 40) first | equipment ranking | **Partial** — documented; ranking improvement deferred |
| Live DSA/RAA data placement | DNS | **BLOCKED BY DNS** |
| PI pricing deep scenarios | needs PI wallet fixtures | **PARTIAL** — profile field returned; multi-user PI matrix limited on pilot student |
| Absolute &lt;15s portal latency | model/hardware | **Not in scope** (AI.21.2 envelope kept) |

---

## 5. Changes implemented

| Area | Change |
|------|--------|
| `tools._estimate_booking_cost` | Authoritative calculate via existing engines; returns amount/breakdown/`pricing_profile` |
| `portal_grounding` | Pass `num_samples`; broader results/deadline/remote phrases; safer slot wants |
| `query_intelligence.py` | Follow-up enrichment + clarification helpers |
| `conversation.send_message` | Clarification short-circuit (no LLM); enrich before grounding |
| Tests | `test_ai22_intelligence.py` + eval JSON subset |
| Docs | Evaluation dataset + this report |

**Unchanged:** pilot allowlist, Ollama resources, MAX_TOKENS=160, no 3B install.

---

## 6. Controlled live evaluation (pilot only)

### Deterministic

| Check | Result |
|-------|--------|
| Clarification “Can I book it?” | **17 ms**, provider=`deterministic`, asks for equipment |
| Follow-up enrich | prior XRD context attached |
| Pricing tools | `search_equipment` + `estimate_booking_cost` **ok**; portal block contains **amount** |
| Cross-user results | denied (`booking_not_found`) |
| Cancel prepare | `requires_confirmation=true` |
| Prompt injection | no key leak |
| `/api/version` | 200 ~24 ms |
| Allowlist | still only `test.student@…` |

### Live matrix subset

| Query | wall_ms | tools | ok | notes |
|-------|---------|-------|-----|-------|
| What is my next booking? | 34997 | get_next_booking | PASS | |
| How much does 5 XRD samples cost? | **16004** | pricing chain | PASS | was ~50s; now grounded amount |
| Are my results ready? | 15979 | get_booking_results | PASS | |
| What software can I use for PXRD? | 24972 | recommend_software | PASS | |
| When should I submit my sample? | 10241 | get_sample_deadline | PASS | new coverage |
| What is XRD? | 35278 | RAG | PASS | |
| Follow-up: Can I use it remotely? | 15071 | recommend_software | PASS | `followup_enriched=true` |

**SUMMARY:** n=6 core rows, **ok=6**, **timeouts=0**, avg≈22.9s, max≈35.3s.

---

## 7. Quality scorecard (honest)

Measured on: evaluation dataset routing unit expectations + live subset + security regressions.  
**Not** a 99% claim. Labels mix CORRECT / PARTIAL.

| Category | Assessment | Notes |
|----------|------------|-------|
| Booking | **HIGH** | next/list grounded |
| Availability | **MEDIUM-HIGH** | slots chain works; family disambiguation partial |
| Pricing | **HIGH (fixed)** | authoritative engine; watch equipment rank |
| PI pricing | **PARTIAL** | profile exposed; limited pilot fixture |
| Wallet | **MEDIUM** | tool exists; light live coverage this pass |
| Samples | **HIGH** | status + deadline phrases |
| Results | **HIGH** | ready/download phrases; own-scope |
| Software | **HIGH** | no redundant equipment dump |
| Remote Analysis | **PARTIAL** | software path yes; live DSA/RAA **DNS blocked** |
| Equipment | **MEDIUM** | search works; ranking can prefer GI-XRD for “XRD” |
| Knowledge / prep | **MEDIUM-HIGH** | docs tool; KB gaps remain if docs missing |
| General science | **MEDIUM** | 1b + compact RAG; capped tokens |
| Mixed | **PARTIAL** | ≤3 tools; not fully matrixed live |
| Follow-ups | **HIGH (improved)** | enrichment + pricing/remote |
| Clarification | **HIGH (new)** | deterministic success |
| Security / refusal | **HIGH** | cross-user, injection, confirm |
| Timeouts | **HIGH** | 0 on AI.22 subset |

### Aggregate (subset, not population)

| Metric | Estimate |
|--------|----------|
| Useful-answer rate (live subset) | **6/6 ≈ 100%** of tested rows (small n) |
| Safe-answer / refusal | **PASS** on security probes |
| Hallucination risk (pricing) | **Reduced** (was high when estimate null) |
| Tool-selection accuracy (JSON subset routing) | Automated unit coverage added |
| Timeout rate (AI.22 live subset) | **0%** |
| Clarification accuracy | **PASS** on ambiguous book/cost |

Dataset size for full taxonomy: **70** labeled queries (synthetic + pilot-derived + regression). Full human scoring of all 70 against live Ollama was **not** completed in this pass (time/concurrency=1).

---

## 8. Model limitation analysis

After routing/tool/context fixes:

| Remaining weakness | Likely class |
|--------------------|--------------|
| 20–35s latency on CPU 1b | model/hardware (not fixed by more tools) |
| Verbose/awkward phrasing | model reasoning (acceptable if facts correct) |
| XRD→GI-XRD first hit | retrieval/ranking (not 1b incapacity) |
| Deep RA session data Qs | knowledge + DNS / R12 live path |

**Conclusion:** Do **not** install 3B in AI.22. Measured intelligence gaps were primarily **tool/routing/clarification**, not proof that 1b must be replaced.

---

## 9. Acceptance matrix

| Item | Status |
|------|--------|
| Existing capabilities inventoried | **PASS** |
| Query taxonomy created | **PASS** |
| Pilot queries analyzed | **PASS** (bench-heavy; anonymized) |
| Evaluation dataset created | **PASS** (70) |
| Booking coverage | **PASS** |
| Availability coverage | **PARTIAL** |
| Pricing coverage | **PASS** (authoritative) |
| PI pricing coverage | **PARTIAL** |
| Wallet coverage | **PARTIAL** |
| Sample coverage | **PASS** |
| Results coverage | **PASS** |
| Software coverage | **PASS** |
| Remote Analysis coverage | **PARTIAL** / DNS **BLOCKED** for live DSA |
| Equipment coverage | **PARTIAL** (ranking) |
| Knowledge coverage | **PARTIAL** |
| General science coverage | **PARTIAL** |
| External-user questions | **NOT TESTED** (pilot auth only) |
| Mixed questions | **PARTIAL** |
| Follow-up questions | **PASS** |
| Clarification behavior | **PASS** |
| Tool-routing accuracy | **PASS** (improved + tests) |
| Missing-tool analysis | **PASS** |
| Knowledge-gap analysis | **PARTIAL** |
| Cross-user security | **PASS** |
| Prompt-injection protection | **PASS** |
| Confirmation workflow | **PASS** |
| Performance regression | **PASS** (0 timeouts; pricing faster) |
| Automated regression tests | **PASS** (new AI.22 tests) |
| Model limitation analysis | **PASS** |
| Core platform isolation | **PASS** |
| Documentation | **PASS** |

---

## 10. Rollback

```bash
RESEARCH_COPILOT_ENABLED=false
# recreate django
```

Or revert AI.22 Copilot commits and rebuild django. Keep allowlist unchanged.

---

## 11. Verdict

### **READY FOR PILOT CONTINUATION — IMPROVEMENTS REQUIRED**

**Why not expansion yet**

- Full 70-query human quality scorecard not complete.
- Equipment family disambiguation still imperfect.
- PI pricing / external-user / live RAA matrices incomplete.
- Absolute latency targets still CPU-bound.

**Why continuation is justified**

- Critical pricing hallucination path fixed with portal engines.
- Clarification + follow-up intelligence landed without resource growth.
- Security regressions green; pilot still single seeded account.
- Performance envelope from AI.21.2 preserved (0 timeouts on eval subset).
