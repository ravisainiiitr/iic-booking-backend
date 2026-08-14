# AI.20 — Functional Qualification Report

**Date:** 2026-08-14 / 2026-08-15 IST  
**Host:** `3.110.50.174`  
**DNS:** `equip.iitr.ac.in` → `15.206.88.2` (**unchanged**)  
**Ollama:** unchanged from AI.19 (`llama3.2:1b`, 2 CPU / 8 GB, `MAX_CONCURRENT=1`, private)  
**Production flag:** `RESEARCH_COPILOT_ENABLED=false` (**kept OFF**)  
**Pilot allowlist:** empty (**no authorized emails supplied; none invented**)

Companion: [AI.20-Final-Qualification-Report.md](./AI.20-Final-Qualification-Report.md)

---

## Executive decision

### **PARTIAL — PILOT BLOCKED**

Functional Copilot qualification advanced substantially on production (service-layer + Ollama path) while the HTTP feature flag remained **false**.

**Not** declared:

- READY FOR LIMITED PRODUCTION PILOT (blocked: empty allowlist)
- GLOBAL PRODUCTION READY

---

## What AI.20 did **not** change

| Item | Status |
|------|--------|
| Model | still `llama3.2:1b` |
| Ollama CPU/RAM | still 2 / 8 GB |
| Concurrency | still 1 |
| DNS / DSA-RAA hostname | unchanged |
| Global Copilot enable | **not** enabled |

---

## Code fixes discovered during qualification

Production Equipment/Booking PKs are `equipment_id` / `booking_id`, not `id`. AI.20 fixed Copilot search/pricing routing:

1. `structured_search.search_equipment` — use `eq.pk`; tokenize NL queries (e.g. extract `XRD`)
2. `tools._search_equipment` / slot+cost hrefs — emit `id` + `equipment_id` via `pk`
3. `portal_grounding.run_portal_grounding` — chain `estimate_booking_cost` / `search_slots` after equipment resolve
4. `plan_tool_calls` — match “status of my sample”

These are **tool-routing** fixes (not model upgrades), per AI.20 rules.

Hot-copied into running Django for evidence; committed in repo for durable deploy.

---

## Evidence highlights

### Feature flag / auth

| Check | Result |
|-------|--------|
| Env `RESEARCH_COPILOT_ENABLED=false` | PASS |
| Bootstrap `enabled=false` | PASS |
| API gate 503 `research_copilot_disabled` | PASS |
| In-process allowlist: pilot allowed / non-pilot denied | PASS |
| Empty allowlist + enabled ⇒ global | PASS (logic verified; **not** applied in prod) |

### Portal grounding / tools

| Area | Result | Notes |
|------|--------|-------|
| Next booking tool | PASS | `get_next_booking` |
| Sample status planning | PASS after fix | natural phrasing |
| Results / deadline tools | PASS | scoped; no invent when missing |
| Slot search | PASS | requires equipment; XRD resolves to GI-XRD `#40` |
| Software recommend | PASS | catalog data returned |
| Pricing “5 XRD samples” | PASS (safe) | chains search→`estimate_booking_cost`; **estimate=null**, portal calculate authoritative — **no invented INR** |
| PI pricing scenarios | NOT TESTED | needs enabled pilot + real PI fixtures |
| Cross-user results/sample/cancel | PASS | foreign booking `#405` → `booking_not_found` for user A |
| Cancel confirmation | PASS | `requires_confirmation=true` for owner; no silent mutate |
| Ambiguous “book it tomorrow” | PASS | no blind `search_slots` |

### Ollama / isolation

| Check | Result |
|-------|--------|
| send_message via Ollama | PASS (with intermittent 60s timeouts under load → controlled unavailable) |
| Ollama down | PASS | controlled message; version/ready/Celery OK |
| Timeout | PASS | `TimeoutError` → unavailable text; core OK |
| Concurrency busy | PASS | `busy=true` message |
| DB isolation | PASS | `connection.in_atomic_block=False` during `generate` |
| Resource caps | PASS | Ollama ~2 cores / ~1.5 GiB during inference |

### Clients

| Check | Result |
|-------|--------|
| Frontend production build | PASS (`vite build` ~29s) |
| FE respects backend `enabled` | PASS (code: Vite soft-gate; backend authoritative) |
| Android unit tests | PASS (`gradlew test` exit 0) |
| Android assembleDebug | PASS (exit 0) |
| Android → Ollama direct | PASS (none; only `/v1/research-copilot/*`) |
| Live FE/Android chat E2E | NOT TESTED (flag OFF; no pilot creds) |

### Out of scope / blocked

| Check | Result |
|-------|--------|
| Controlled pilot enable | **BLOCKED** (no real pilot emails) |
| DSA/RAA live under Copilot | **BLOCKED BY DNS** |
| Result-processing soak | NOT TESTED (unsafe to force on prod) |
| Authenticated booking create latency matrix | PARTIAL (version/ready proxy only) |

---

## Query quality (honest)

Do **not** claim 99% accuracy.

Observed:

- **Safe refusals** on pricing invention and injection-style wallet reveal: good
- **1b quirks:** e.g. FWHM explained poorly / confused with portal wording — model quality PARTIAL; improve prompts/KB before considering 3b
- **Timeouts** under serial load at 60s: handled safely; keep `MAX_CONCURRENT=1`

---

## Pilot readiness checklist (remaining)

1. Operator supplies **real** `RESEARCH_COPILOT_PILOT_EMAILS`
2. Durable image deploy of AI.20 Copilot fixes (not only hot-copy)
3. Enable flag **only** with allowlist non-empty
4. Re-run live HTTP matrix for pilot vs non-pilot
5. Optional: DNS cutover then DSA/RAA coexistence

Until then: keep **`RESEARCH_COPILOT_ENABLED=false`**.
