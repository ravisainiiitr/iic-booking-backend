# AI.23 — Pilot Expansion Plan

**Status:** PREPARED — **NOT EXECUTED**  
**Prerequisite report:** [`AI.23-Final-Operational-Qualification.md`](./AI.23-Final-Operational-Qualification.md)

This document answers *how* to expand safely if/when authorized.  
It does **not** add users, change DNS, create PI profiles, or enable Copilot globally.

---

## Current state

```text
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
```

Global enablement: **NO**  
Recommended next size: **3–5 authorized test users** (allowlist-only)

---

## Expansion decision gate

Expand only after **explicit human approval** and checklist:

| Gate | Required |
|------|----------|
| AI.23 quality baseline still holds (useful/safe/hallucination/timeout) | YES |
| No open security incident | YES |
| Envelope still frozen (1b / 2 CPU / 8 GB / concurrent 1 / tokens 160) | YES |
| Each new email individually approved | YES |
| Documented caveats accepted if PI still unconfigured | YES (or configure PI via admin UI first) |
| Documented caveats accepted if DNS still wrong for live RAA | YES (or fix DNS first) |

**Still out of scope:** domain-wide allowlist, wildcards, automatic faculty inclusion, `RESEARCH_COPILOT_PILOT_EMAILS=""`.

---

## Recommended configuration (when approved)

```text
RESEARCH_COPILOT_ENABLED=true
RESEARCH_COPILOT_PILOT_EMAILS=
  test.student@iic-booking.test,
  <approved-user-2@...>,
  <approved-user-3@...>
```

Rules:

- Explicit emails only (comma-separated)  
- Server-side `feature_enabled()` remains authoritative  
- No frontend-only gating  

---

## Suggested pilot cohort shape (3–5)

| Slot | Role intent | Known fixtures |
|------|-------------|----------------|
| 1 | Existing student test | `test.student@iic-booking.test` (already live) |
| 2 | Faculty / wallet-owner style | Known wallet + bookings |
| 3 | External-style or limited booking user | Public/catalog questions + auth boundaries |
| 4–5 | Optional extras | Same instrumentation domains (XRD/SEM) for comparable scenarios |

Each user must have: known identity, known test data, named scenarios, owner for support.

---

## Scenario matrix per pilot user

| Category | Example prompts |
|----------|-----------------|
| Booking | Next booking; list my bookings |
| Pricing | 5 PXRD samples cost; clarify bare XRD |
| PI (if configured) | Do I get the PI rate? — expect resolver meta, not LLM judgment |
| Samples / deadlines | Sample status; submission deadline |
| Results | Own results only; foreign id denied |
| Software / RA (code-level) | Software for PXRD; analyze remotely |
| Live RAA | Only if DNS → current EC2 and heartbeats green |
| Follow-up / clarify | Pronoun follow-ups; ambiguous book/cost |
| Security | Prompt injection; other-user data; secret URL |

---

## Domain caveats (must be in pilot brief)

1. **Production PI ChargeProfiles = 0** until admin configures via existing admin UI → live PI *amount* differences may not appear; resolver correctly stays on standard.  
2. **Live RAA BLOCKED — DNS** until `equip.iitr.ac.in` points at `3.110.50.174` → do not promise live session placement.  
3. Latency p95 may remain ~30–40s on CPU 1b for some LLM turns; deterministic portal paths are faster.

---

## Rollout steps (manual)

1. Confirm AI.23 (or newer) scorecard still green on canary account.  
2. Obtain written approval for each email.  
3. Update `RESEARCH_COPILOT_PILOT_EMAILS` only (no global flag change).  
4. Recreate/reload django env as per existing deploy practice.  
5. Verify each new user: allow; verify one non-listed user: deny.  
6. Run abbreviated scenario matrix (not necessarily full 86) per user.  
7. Watch timeout/error/busy rates for 24–48h.

---

## Rollback

```text
RESEARCH_COPILOT_ENABLED=false
```

Or shrink allowlist back to:

```text
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
```

Optional: stop Ollama. Booking/DSA/RAA/Celery unaffected.

---

## Monitoring during expansion

Warn: timeout > 2%, error > 2%.  
Pause pilot on any security failure or verified hallucination.

---

## Explicit non-actions for AI.23

- [x] Plan prepared  
- [ ] Users added ← **not done**  
- [ ] Global enablement ← **not done**  
- [ ] DNS changed ← **not done**  
- [ ] Production PI invented ← **not done**  
- [ ] 3B / resource increase ← **not done**
