# AI.22.2 — Remaining Gaps

**Companion to:** [`AI.22.2-Final-Qualification-Report.md`](./AI.22.2-Final-Qualification-Report.md)

## Closed in AI.22.2

| Gap (from AI.22.1) | Resolution |
|--------------------|------------|
| 1/86 timeout (Q-U-001 mixed cost+prepare) | Deterministic portal synthesis + prompt compaction; **0 timeouts** on final 86 |
| Q-V-003 follow-up equipment listing near timeout | Deterministic equipment list path / faster follow-ups |
| Ollama URL / secret probe could fabricate (Q-X-005) | Deterministic `security_refusal` |
| Missing booking/equipment could be narrated as found | Deterministic not-found from tool errors |

## Still open (block full “ready for expansion”)

### 1. Production PI configuration — NOT CONFIGURED

- `ChargeProfile` with `pricing_profile=pi`: **0** rows in production.  
- Copilot / resolver correctly falls back to **standard**.  
- Live “PI amount differs from standard” cannot be claimed until an authorized admin creates PI profiles.  
- **Do not** invent production PI rows for Copilot green bars.

### 2. Live Remote Analysis — BLOCKED — DNS

- `equip.iitr.ac.in` → `15.206.88.2`  
- Current EC2 public IP → `3.110.50.174`  
- Code-level RA/software answers remain usable; **live** DSA/RAA/Guacamole E2E **must not** be marked PASS.

### 3. Pilot policy (intentional)

- Global enablement: **NO**  
- Allowlist: **single** test account only  
- Expansion requires evidence after PI + DNS gates — not automatic from this phase

### 4. Residual quality notes (non-blocking for safety)

- One **PARTIALLY_CORRECT** row in final set (location phrasing for FESEM).  
- Small CPU 1b still has high p95 (~39s) on some LLM turns — acceptable vs inventing answers; do not raise timeout to “fix” quality.  
- Conversation follow-ups that mention bare “XRD” still correctly **clarify** PXRD vs GI-XRD (success, not a failure).

## Explicit non-goals remaining

- No automatic pilot expansion  
- No global Copilot enablement  
- No automatic 3B install  
- No production PI profile fabrication  
- No live RAA PASS claim while DNS blocks
