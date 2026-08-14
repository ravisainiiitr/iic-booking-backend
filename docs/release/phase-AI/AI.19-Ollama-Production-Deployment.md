# AI.19 — Ollama Production Deployment & Qualification Report

**Date:** 2026-08-14 / 2026-08-15 IST  
**Host:** `3.110.50.174` (`ip-10-0-1-153`, m5a.2xlarge)  
**DNS:** `equip.iitr.ac.in` still → `15.206.88.2` (**unchanged; intentionally not modified**)  
**Copilot flag:** `RESEARCH_COPILOT_ENABLED=false` (**kept OFF**)  
**Pilot allowlist:** empty (**no invented accounts**)

Related baseline: [AI.19-EC2-Ollama-Baseline.md](./AI.19-EC2-Ollama-Baseline.md)

---

## Executive verdict

| Area | Verdict |
|------|---------|
| Ollama private install + `llama3.2:1b` on new EC2 | **PASS** |
| Existing Research Copilot → Ollama provider wiring | **PASS** (provider path live; feature flag OFF) |
| Core platform isolation during inference / Ollama down | **PASS** (version/ready unaffected) |
| Controlled pilot enablement | **BLOCKED** (empty allowlist; qualification incomplete for live portal tools) |
| DNS / live DSA–RAA heartbeat E2E | **BLOCKED BY DNS** / **NOT TESTED** |
| AI.19 overall | **PARTIAL** — infrastructure ready; Copilot remains OFF |

**Do not claim AI.19 complete for live Copilot pilot.** Ollama is installed and the existing Copilot stack is configured to use it, but the global feature flag stays false until authorized pilot emails exist and portal-tool live gates are executed with Copilot temporarily enabled for those accounts only.

---

## Architecture (unchanged Copilot; Ollama as inference)

```
Android / Frontend
        │
        ▼
Django Research Copilot API  (feature flag gated)
        │
        ▼
OllamaGateway (AI.17)  →  http://ollama:11434  (Docker network only)
        │
        ▼
llama3.2:1b
```

- No second Copilot engine.
- No public Ollama API.
- No OpenAI key required (`OPENAI_KEY_SET=False` in production container).
- DSA/RAA clients remain hostname-based (`https://equip.iitr.ac.in`).

---

## EC2 capacity

| Resource | Value |
|----------|-------|
| Instance | m5a.2xlarge |
| vCPU | 8 |
| RAM | ~30.7 GiB |
| Disk | 243G filesystem (~16% used at baseline) |
| GPU | none (CPU-only inference) |

---

## Ollama deployment

| Item | Value |
|------|-------|
| Install method | Official `ollama/ollama` Docker image |
| Compose overlay | `docker-compose.ollama.production.yml` |
| Container | `iic-booking-backend-ollama-1` |
| Ollama version | `0.32.11` |
| Network | `iic-booking-backend_default` (alias `ollama`) |
| Published ports | **none** (`ports` null; 11434 not on host) |
| CPU limit | `2.0` vCPU (`NanoCpus=2000000000`) |
| RAM limit | `8g` |
| Restart | `unless-stopped` |
| Model | `llama3.2:1b` (`baf6a787fdff`, **1.3 GB**) |
| Django endpoint | `OLLAMA_BASE_URL=http://ollama:11434` |
| Provider | `COPILOT_PROVIDER=ollama` / `COPILOT_LLM_PROVIDER=ollama` |
| Concurrency | `RESEARCH_COPILOT_MAX_CONCURRENT=1` |
| Enabled | `RESEARCH_COPILOT_ENABLED=false` |

### Compose default fix (required)

`docker-compose.production.yml` `environment:` interpolation was overriding `.envs/.production/.django` when host env vars were empty. Defaults were corrected to:

- `OLLAMA_BASE_URL` → `http://ollama:11434`
- `OLLAMA_MODEL` → `llama3.2:1b`
- `RESEARCH_COPILOT_MAX_CONCURRENT` → `1`

---

## Network security

| Check | Result |
|-------|--------|
| Host listen on 11434 | **none** |
| Docker published 11434 | **none** |
| Probe `3.110.50.174:11434` from EC2 | `CLOSED_OR_FILTERED` |
| Probe from operator workstation | `TcpTestSucceeded=False` |
| Postgres 5432 / Redis 6379 public | **not reachable** |
| AWS SG change to expose Ollama | **not performed** |

---

## Inference benchmark (`llama3.2:1b`, via `OllamaGateway`)

Captured 2026-08-14 ~19:15–19:18 UTC. Timeouts: **0**.

| Suite | N | OK | Avg | P50 | Max |
|-------|---|----|-----|-----|-----|
| short | 10 | 10 | 2013 ms | 1914 ms | 3302 ms |
| medium | 5 | 5 | 16496 ms | 10273 ms | 34011 ms |
| copilot-style | 5 | 5 | 5999 ms | 5182 ms | 10223 ms |

Concurrent pair (direct Ollama, not Copilot gate): wall **3010 ms**; both completed (Ollama may serialize internally).

### Resource peaks during benchmark

| Metric | Peak observed |
|--------|----------------|
| Ollama CPU | ~**203%** of one host CPU (~2.0 cores — at configured cap) |
| Ollama RAM | ~**1.56 GiB / 8 GiB** |
| Host load (after) | 3.54, 1.69, 0.80 |
| Host MemAvailable | still ~25–26 GiB |

**Model decision:** remain on **`llama3.2:1b`**. Do **not** install `3b` yet — no quality inadequacy evidence under tool-grounded Copilot design, and CPU is already saturating the 2-vCPU cap on longer answers.

---

## Copilot provider integration (flag OFF)

| Check | Evidence | Result |
|-------|----------|--------|
| Gateway class | `OllamaGateway` | PASS |
| Health | `available` / `reachable` (~65–81 ms) | PASS |
| Model | `llama3.2:1b` pulled | PASS |
| OpenAI required | `OPENAI_KEY_SET=False` | PASS |
| Bootstrap `enabled` | `False` for authenticated user | PASS |
| Mutating gate | `_feature_gate` → **503** `research_copilot_disabled` | PASS |
| Pilot emails | empty string | PASS (correctly empty) |

---

## Failure isolation

| Scenario | Copilot/Ollama | Core platform |
|----------|----------------|---------------|
| Ollama stopped | health `unavailable` / `network`; complete returns empty + error category | `/api/version` 200 ~23 ms; `/api/v1/analysis/health/ready/` 200 ~70 ms |
| Ollama restarted | health recovers; complete returns `OK.` (~5.4 s) | unaffected |

---

## Concurrency gate

With `MAX_CONCURRENT=1`:

- first `acquire_generation_slot` holds slot
- second acquire raises **`CopilotBusyError`**
- `rejected_total` increments

Live dual Copilot HTTP busy behavior with flag ON: **NOT TESTED** (flag remains false).

---

## Booking / core latency during inference

Unauthenticated booking list URL used in scripts returned 404 (routing), so latency proxy used **`/api/version`** and **`/api/v1/analysis/health/ready/`** while six background generations ran.

| Probe | Baseline | During inference |
|-------|----------|------------------|
| `/api/version` | ~22–23 ms | ~22–27 ms |
| analysis ready | ~64–72 ms | ~67–104 ms (one sample 104 ms) |

**Interpretation:** no material starvation observed under this controlled load. Full authenticated booking create/cancel latency matrix: **PARTIAL / NOT TESTED** (no disruptive booking mutations performed on production).

---

## Celery

- `inspect ping` → pong before and after benchmark
- active queue empty before benchmark
- celeryworker memory stable (~450–475 MiB) while Ollama peaked

Live booking-completion / notification task timing under load: **PARTIAL**.

---

## DSA / RAA

| Check | Result |
|-------|--------|
| Workstation rows present | 3 |
| Heartbeats | **STALE** (e.g. RAVI ~2.3 h, CSMH6BU ~6.9 h at check time) |
| Live JOIN / sync while Copilot runs | **BLOCKED BY DNS** (agents still target `equip.iitr.ac.in` → old IP) |
| Hostname architecture change | **not done** (correct) |

---

## Portal grounding / tools / security / pricing / mutations

Existing server-side implementation remains in tree (`portal_grounding`, `TOOL_REGISTRY`, `requires_confirmation`, AI.13/AI.14/AI.17 tests).

**Live production execution with Copilot enabled:** **NOT TESTED** — intentionally, because:

1. `RESEARCH_COPILOT_ENABLED=false`
2. no authorized pilot emails supplied
3. must not invent credentials

Registry confirmed present on production image, including:

`search_slots`, `get_wallet`, `get_next_booking`, `get_sample_status`, `get_booking_results`, `get_sample_deadline`, `estimate_booking_cost`, `recommend_software`, mutating tools with confirmation flags.

---

## Frontend / Android

| Surface | Result |
|---------|--------|
| Frontend live Copilot UI states | **NOT TESTED** on this pass (Copilot OFF; no FE change required for Ollama) |
| Android ↔ Ollama direct | Architecture requires Django only; Android tree not present in this workspace for re-scan → **NOT TESTED** this pass |
| Backend disabled controls UI | bootstrap `enabled:false` + 503 gate → **PASS** (server contract) |

---

## Monitoring (minimal)

Observed via `docker stats`, Ollama health, gateway latency fields, concurrency snapshot (`active_generations`, `rejected_total`).

Sensitive secrets were not logged. Dedicated Prometheus exporters for Copilot: **PARTIAL** (ops can extend later).

---

## Rollback

1. Keep / set `RESEARCH_COPILOT_ENABLED=false` (already).
2. If pressure: `docker stop iic-booking-backend-ollama-1` (verified core stays up).
3. Optional remove: `docker compose -f docker-compose.ollama.production.yml down` (volume retained unless explicitly removed).

---

## Controlled pilot

**BLOCKED.**

Required before enablement:

1. Real authorized emails in `RESEARCH_COPILOT_PILOT_EMAILS`
2. Live portal-tool / grounding / injection / confirmation matrix with flag true for those emails only
3. Re-measure booking + Celery under that pilot
4. Prefer DNS cutover complete before claiming RAA/DSA coexistence under real agent traffic

**Do not set `RESEARCH_COPILOT_ENABLED=true` as part of AI.19 closeout.**

---

## Acceptance matrix

| Item | Status | Evidence |
|------|--------|----------|
| EC2 resource verification | **PASS** | 8 vCPU / ~30 GiB / 250G |
| Pre-Ollama baseline | **PASS** | `AI.19-EC2-Ollama-Baseline.md` |
| Ollama installation | **PASS** | Docker image + compose overlay |
| Ollama service | **PASS** | container up, restart policy |
| CPU-only inference | **PASS** | no GPU/CUDA |
| llama3.2:1b | **PASS** | listed 1.3 GB |
| Model benchmark | **PASS** | 20/20 + 5 copilot-style |
| CPU benchmark | **PASS** | ~203% ≈ 2-core cap |
| RAM benchmark | **PASS** | ~1.56 GiB / 8 GiB |
| Network isolation | **PASS** | no public 11434 |
| Copilot provider integration | **PASS** | `OllamaGateway` + settings |
| Copilot authentication | **PARTIAL** | disabled gate PASS; live chat auth path NOT TESTED while OFF |
| Portal grounding | **NOT TESTED** | flag OFF |
| Booking tools | **NOT TESTED** | flag OFF (registry present) |
| Wallet tools | **NOT TESTED** | flag OFF |
| Sample tools | **NOT TESTED** | flag OFF |
| Results tools | **NOT TESTED** | flag OFF |
| Software tools | **NOT TESTED** | flag OFF |
| Pricing tools | **NOT TESTED** | flag OFF |
| Confirmation workflow | **PARTIAL** | code/registry `requires_confirmation`; live NOT TESTED |
| Authorization | **PARTIAL** | prior AI.13 tests + disabled gate; live cross-user NOT TESTED |
| Prompt injection protection | **NOT TESTED** | live; prior unit coverage exists in repo |
| Concurrency gate | **PASS** | `CopilotBusyError` at max=1 |
| Timeout handling | **PARTIAL** | controlled errors on Ollama down; explicit long-timeout soak NOT TESTED |
| Ollama failure recovery | **PASS** | stop/start recover |
| Booking latency | **PARTIAL** | version/ready proxy OK; full booking APIs NOT TESTED |
| Celery impact | **PARTIAL** | ping OK; deep task latency NOT TESTED |
| DSA impact | **BLOCKED BY DNS** | heartbeats stale |
| RAA impact | **BLOCKED BY DNS** | heartbeats stale |
| Result processing impact | **NOT TESTED** | no result job exercised |
| Frontend | **NOT TESTED** | live UI |
| Android | **NOT TESTED** | this workspace |
| Monitoring | **PARTIAL** | docker stats + health |
| Rollback | **PASS** | flag OFF + Ollama stop proven |
| Controlled pilot | **BLOCKED** | empty allowlist |

---

## Known limitations

1. DNS still points to old EC2 — not an AI.19 blocker for Ollama, but blocks live DSA/RAA coexistence proof.
2. Copilot remains globally disabled — correct for safety.
3. Authenticated booking create/cancel latency under inference not measured on production.
4. Query-quality evaluation set for portal tools not executed live (requires pilot enablement).
5. `llama3.2:3b` not installed (by design until 1b proven inadequate).

---

## Ops commands (reference)

```bash
# Start/ensure private Ollama
cd /home/ubuntu/iic-booking-backend
docker compose -f docker-compose.ollama.production.yml up -d

# Status
docker ps --filter name=ollama
docker exec iic-booking-backend-ollama-1 ollama list

# Emergency stop (Copilot degrades; core stays)
docker stop iic-booking-backend-ollama-1

# Keep Copilot OFF
# RESEARCH_COPILOT_ENABLED=false in .envs/.production/.django
```
