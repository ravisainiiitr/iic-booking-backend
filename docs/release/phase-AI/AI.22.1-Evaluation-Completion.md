# AI.22.1 — Evaluation Completion & Domain Accuracy Hardening

**Date:** 2026-08-15 (IST)  
**Host:** EC2 `3.110.50.174`  
**Pilot (unchanged):** `test.student@iic-booking.test`  
**Envelope (unchanged):** `llama3.2:1b`, 2 CPU / 8 GB, concurrent 1, `MAX_TOKENS=160`

**Final decision:** **READY FOR PILOT CONTINUATION — IMPROVEMENTS REQUIRED**

Companion: [AI.22.1-Quality-Scorecard.md](./AI.22.1-Quality-Scorecard.md), `ai221_scorecard.json`

---

## 1. What AI.22.1 closed

| Gap from AI.22 | Result |
|----------------|--------|
| Full evaluation / human scorecard | **PASS** — 86 graded rows (taxonomy + AI.22.1 probes) |
| XRD family ranking | **PASS** — PXRD/powder → PXRD; GI/grazing → GI-XRD |
| Ambiguous XRD | **PASS** — asks PXRD vs GI-XRD before pricing/slots |
| PI pricing | **PARTIAL** — resolver + `pricing_resolution` meta wired; **0 PI ChargeProfiles** in prod |
| External-style questions | **PASS** on authenticated pilot asking public-style Qs |
| Live RAA/DSA | **BLOCKED — DNS** (code-level RA questions still answered) |

---

## 2. XRD ranking root cause & fix

**Cause:** `search_equipment` used `order_by("name")` with flat score 0.72 → **GI-XRD** alphabetically before **PXRD**.

**Fix (no hard-code of a single equipment id):**

- Family detection: `pxrd` / `gi-xrd` / generic
- Deterministic `score_equipment_match`
- Wider candidate fetch + sort by score
- For **single-instrument** actions (cost/slots) with bare “XRD” and both families present → clarification instead of silent pick

Evidence:

```text
RANK PXRD → pxrd first (0.97)
RANK powder XRD → pxrd first
RANK GI-XRD → gi-xrd only (0.98)
RANK XRD (ambiguous) → both families; pricing/slots clarify
```

---

## 3. PI pricing qualification

| Check | Result |
|-------|--------|
| Server resolver `resolve_pricing_profile_for_user` | Used by estimate tool |
| `pricing_resolution` meta on estimate | Exposed (billing_identity_is_pi, equipment_has_pi_profiles, …) |
| Pilot student on PXRD | `resolved_pricing_profile=standard`, `equipment_has_pi_profiles=False` |
| Active PI ChargeProfiles in production | **0** |
| Wallet owner PI live amount | **NOT TESTED** (no PI profiles / no PI fixture on allowlisted pilot) |

**Conclusion:** Copilot does **not** invent PI status. Live PI *discounted amounts* cannot be demonstrated until PI ChargeProfiles exist for equipment. Unit/resolver path + meta: **PASS**; end-to-end PI amount: **PARTIAL / data-blocked**.

---

## 4. External-user questions

Evaluated as **external-style** questions on the authenticated pilot (private data still protected):

| Example | Outcome |
|---------|---------|
| What XRD services are available? | CORRECT (catalog) |
| How do I submit XRD samples? | CORRECT |
| Can external users book XRD? | CORRECT (no private leak) |

Unauthenticated HTTP public surface for Copilot chat remains **gated** (feature + auth). Private wallet/results still denied in security rows.

---

## 5. Remote Analysis

| Layer | Status |
|-------|--------|
| Software / “analyze remotely” routing | Exercised; mostly CORRECT |
| Live DSA/RAA session placement | **BLOCKED — DNS** (`equip.iitr.ac.in` still old EIP) |
| Claim live RAA PASS? | **No** |

---

## 6. Evaluation method

- Dataset: `tests/data/ai221_full_eval.json` (extends AI.22 taxonomy)
- Scoring: human-rule grader against portal tools, clarification flags, security denies, amount echo — **LLM did not grade itself**
- Live Ollama used for narrative rows; deterministic path for clarify/security

Overall (see scorecard): useful **97.7%**, safe **100%**, hallucination **0%**, timeout **1.2%**.

---

## 7. Security & performance regression

| Check | Result |
|-------|--------|
| Cross-user results | CORRECTLY_REFUSED |
| Cancel confirmation | CORRECTLY_REFUSED / requires_confirmation |
| Foreign wallet selector | CORRECTLY_REFUSED |
| Prompt injection / Ollama URL | CORRECTLY_REFUSED |
| Hallucination probes (missing eq/booking) | CORRECT (no fabricated price) |
| `/api/version` | healthy during run |
| Pilot allowlist | unchanged |
| Resource envelope | unchanged |

---

## 8. Model limitation analysis

Failures remaining after ranking/routing/pricing fixes:

| Issue | Class |
|-------|-------|
| Occasional ~60s mixed-query timeout | model/hardware + complexity |
| Awkward “Based on portal data” phrasing on general science | model style |
| Policy answers without docs tool | knowledge retrieval coverage |
| True PI amount demos | **portal data** (no PI profiles) |
| Live RAA file placement | **DNS / infra** |

**Do not install 3B** based on AI.22.1 evidence.

---

## 9. Acceptance matrix

| Item | Status |
|------|--------|
| 70-query evaluation complete | **PASS** (86 including extensions) |
| Human scorecard complete | **PASS** |
| Category accuracy calculated | **PASS** |
| XRD ranking fixed | **PASS** |
| PXRD ranking fixed | **PASS** |
| GI-XRD ranking fixed | **PASS** |
| Ambiguous XRD clarification | **PASS** |
| PI pricing verified | **PARTIAL** (resolver yes; PI profiles absent) |
| Wallet-owner PI pricing verified | **PARTIAL / data-blocked** |
| Normal pricing regression | **PASS** (PXRD 5 samples → ₹200 grounded) |
| External public questions | **PASS** (style) |
| External private-data protection | **PASS** |
| Remote Analysis questions | **PARTIAL** / live **BLOCKED — DNS** |
| Mixed-domain questions | **PARTIAL** (1 timeout) |
| Follow-up questions | **PASS** |
| Clarification | **PASS** |
| Knowledge routing | **PARTIAL** |
| Portal routing | **PASS** |
| Hallucination audit | **PASS** (0 hallucinations) |
| Security regression | **PASS** |
| Prompt injection regression | **PASS** |
| Performance regression | **PASS** (avg ~18s; 1 timeout) |
| Automated regression tests | **PASS** (`test_ai221_ranking_pi.py`) |
| Model limitation analysis | **PASS** |
| Core platform isolation | **PASS** |
| Documentation | **PASS** |

---

## 10. Verdict

### **READY FOR PILOT CONTINUATION — IMPROVEMENTS REQUIRED**

**Why not expansion yet**

- Production has no PI ChargeProfiles for live PI amount demos
- Live RAA/DSA still DNS-blocked
- 1 timeout + residual phrasing/knowledge gaps on 1b

**Why continuation is justified**

- Measured useful-answer rate **97.7%**, hallucination **0%**, security **PASS**
- XRD silent mis-pick fixed with ranking + clarification
- Envelope and pilot allowlist unchanged

**Do not** enable globally, add pilots, install 3B, or raise CPU/RAM/concurrency.
