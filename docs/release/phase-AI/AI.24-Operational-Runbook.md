# AI.24 — Operational Runbook

**Audience:** operators / admins for Research Copilot limited pilot  
**Baseline:** AI.23 golden + AI.24 gate  
**Policy:** allowlist only — **never** global unrestricted enablement

---

## 1. Current production posture

| Item | Value |
|------|-------|
| Flag | `RESEARCH_COPILOT_ENABLED=true` |
| Allowlist | `RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test` |
| Model | `llama3.2:1b` |
| Ollama resources | 2 CPU, 8 GB |
| Concurrency | `RESEARCH_COPILOT_MAX_CONCURRENT=1` |
| Max tokens | `RESEARCH_COPILOT_MAX_TOKENS=160` |
| LLM timeout | 60s |
| PI profiles | **0 — NOT CONFIGURED** |
| Live RAA | **BLOCKED — DNS** (`equip.iitr.ac.in` → `15.206.88.2`; EC2 `3.110.50.174`) |

**Do not change** model / CPU / RAM / concurrency / max tokens without a new qualification phase.

---

## 2. Daily health checks

```bash
curl -sS -m 5 http://127.0.0.1:8080/api/version
curl -sS -m 5 http://127.0.0.1:8080/api/v1/analysis/health/ready/
curl -sS -m 5 http://127.0.0.1:8080/api/v1/analysis/health/live/
docker exec iic-booking-backend-django-1 celery -A config.celery_app inspect ping -t 5
docker exec iic-booking-backend-ollama-1 ollama list
docker exec iic-booking-backend-django-1 printenv | grep RESEARCH_COPILOT
```

Expect: version/ready/live HTTP 200; Celery pong; `llama3.2:1b` present; allowlist unchanged unless approved.

---

## 3. Priority under resource pressure

```text
Booking > Celery > DSA > RAA > Result processing > Copilot
```

If the host is constrained, Copilot must degrade/fail first (`busy` / provider errors). Booking and analysis readiness must remain healthy.

---

## 4. Monitoring thresholds

| Metric | Warn | Pause pilot |
|--------|------|-------------|
| Timeout rate | > 2% | — |
| Copilot error rate | > 2% | — |
| Verified hallucination | investigate | if price/booking/user/system state |
| Authz bypass / cross-user data / secrets | — | **immediate** |
| Unauthorized mutation | — | **immediate** |

Privacy: do not log tokens, passwords, API keys, or unnecessary private booking bodies.

---

## 5. Failure recovery

### Ollama unavailable

1. Users see controlled “temporarily unavailable… booking unaffected”.  
2. Confirm `/api/version` and analysis ready still 200.  
3. Restart Ollama container; confirm `ollama list` and a canary Copilot message.

### Busy gate

With `MAX_CONCURRENT=1`, overload yields busy rejection — expected.

### Suspected security issue

```text
RESEARCH_COPILOT_ENABLED=false
```

Recreate/reload django so the flag applies. Confirm allowlisted users lose Copilot; booking APIs unaffected.

---

## 6. Rollback procedure

**Primary:**

```text
RESEARCH_COPILOT_ENABLED=false
```

Apply via env / compose, recreate django (and celery if it caches settings).

**Optional:** stop Ollama container (frees CPU/RAM; does not affect booking core).

**Shrink allowlist** (continue single canary):

```text
RESEARCH_COPILOT_PILOT_EMAILS=test.student@iic-booking.test
```

Rollback does **not** require reverting Booking, DSA, RAA, Celery, or result-processing deploys.

---

## 7. Controlled allowlist expansion (manual only)

1. Collect approved emails + purpose + caveats (PI/DNS).  
2. Update `RESEARCH_COPILOT_PILOT_EMAILS` to explicit comma-separated list (3–5 max without new review).  
3. Reload django.  
4. Verify each new email allowed; one outsider denied; unauth 401.  
5. Run matrix A–H from [`AI.24-Pilot-Expansion-Qualification.md`](./AI.24-Pilot-Expansion-Qualification.md).  
6. Watch metrics 24–48h.

**Never:** wildcards, empty allowlist, domain-wide inclusion.

---

## 8. PI configuration (admin UI only)

When authorized:

1. Assign Equipment PI via existing admin.  
2. Create PI ChargeProfiles for required user types.  
3. Verify `pricing_resolution_meta` → `pi` for that identity.  
4. Ask Copilot pricing questions — amounts from ChargeCalculationEngine only.

Until then: report **NOT CONFIGURED**; expect `standard` fallback.

---

## 9. DNS / RAA

Read-only check:

```bash
getent hosts equip.iitr.ac.in
```

If still `15.206.88.2` → **do not** claim live RAA PASS.  
If/when → `3.110.50.174`, follow AI.23/AI.24 live gate (HTTPS smoke, heartbeats, then controlled session) — **do not edit DB**.

---

## 10. Contacts / ownership

| Topic | Owner |
|-------|-------|
| Allowlist changes | Explicit admin approval required |
| PI ChargeProfiles | Equipment / finance admin via existing UI |
| DNS | Infrastructure (not Copilot deploy) |
| Copilot pause | Any security on-call → set `ENABLED=false` |
