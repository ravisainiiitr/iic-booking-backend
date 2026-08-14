# EC2 IP Migration Report

**Status:** Phase 1–4 inventory complete; **Phase 5A applied on new EC2**.  
**DNS CUTOVER = PENDING** (`equip.iitr.ac.in` still → `15.206.88.2`)  
**Date:** 2026-08-14 (UTC) / 2026-08-15 IST  
**Operator machine:** Cursor host with existing PEM `~\.ssh\iic-ec2-deploy.pem`

---

## Phase 5A — New EC2 preparation (DNS still pending)

### Changes applied (new host only)

| Item | Before | After |
|------|--------|-------|
| Env `RA_TUNNEL_GATEWAY_WSS_URL` | `ws://ec2-15-206-88-2…:7090/tunnel` | `ws://ec2-3-110-50-174.ap-south-1.compute.amazonaws.com:7090/tunnel` |
| DB `RemoteAnalysisSettings.tunnel_gateway_wss_url` | old EC2 hostname | **same new EC2 public DNS** |
| Env `DJANGO_ALLOWED_HOSTS` | …`15.206.88.2`… | …`3.110.50.174`,`15.206.88.2`… (old kept for rollback window) |
| Django container | recreated to load env | healthy |
| GitHub secret `EC2_HOST` (`iic-booking-backend`) | prior value | **`3.110.50.174`** (updated 2026-08-14T18:56:07Z) |
| GitHub secret `EC2_HOST` (`iic-booking-frontend`) | prior value | **`3.110.50.174`** (updated 2026-08-14T18:56:08Z) |
| Repo default `ALLOWED_HOSTS` in `config/settings/production.py` (deploy tree) | default old IP | default **`3.110.50.174`** |
| Env backup | — | `.django.bak.phase5a-20260814T185609Z` |

**Not changed:** DNS, DSA/RAA/Android/Frontend portal hostnames (`equip.iitr.ac.in`), Copilot (OFF), Ollama (not installed), certificates.

### Health after change

| Check | Result |
|-------|--------|
| analysis `/ready/` | **ready** (gateway + guacamole ok) |
| `/api/version` | **200** |
| frontend `:8000` | **200** |
| Apache Host `3.110.50.174` | **200** |
| Port 7090 listening | **yes** |
| Effective overlay WSS | new EC2 DNS |

### RAA / DSA live evidence

| Check | Result |
|-------|--------|
| RAA tunnel live E2E (JOIN from lab PC) | **NOT TESTED** — no authorized live join exercised in this phase |
| Agent heartbeats at check | **STALE** (CSMH6BU ~6h+, RAVI ~1.8h+) — expected while public DNS still points at dead old IP for `equip.iitr.ac.in` portal URL used by agents |
| DSA sync live | **NOT TESTED** |

### Phase 5A acceptance matrix

| Criterion | Status |
|-----------|--------|
| New EC2 reachable | **PASS** |
| 8 vCPU / ~30 GiB / 250 GB | **PASS** |
| Docker / Django / Celery / Redis / Guacamole | **PASS** |
| New tunnel endpoint configured | **PASS** |
| RemoteAnalysisSettings updated | **PASS** |
| ALLOWED_HOSTS accepts new IP | **PASS** |
| GitHub `EC2_HOST` updated | **PASS** |
| Deployment target points to new EC2 | **PASS** (secret) |
| RAA tunnel verified | **NOT TESTED** |
| Resource baseline recorded | **PASS** → `EC2-Post-Upgrade-Baseline.md` |
| Copilot OFF / Ollama not installed | **PASS** |
| DNS intentionally unchanged | **PASS** (`DNS CUTOVER = PENDING`) |

### Overall Phase 5A: **PARTIAL PASS**

Ready for DNS cutover checklist; not finished until hostname smoke + live RAA tunnel evidence.

### DNS cutover checklist (later)

1. New EC2 healthy  
2. Tunnel endpoint = new EC2  
3. API/frontend/Celery/Redis/Guacamole healthy  
4. GitHub `EC2_HOST` = new IP  
5. Django accepts new IP  
6. Change DNS A `equip.iitr.ac.in` → `3.110.50.174`  
7. Hostname smoke: version/live/ready/login  
8. Confirm RAA/DSA heartbeats resume  
9. Remove `15.206.88.2` from ALLOWED_HOSTS after rollback window  

---

## Earlier phases (1–4 inventory) — retained below

**Original Phase 1–4 status note:** Inventory + SSH verification were completed before Phase 5A mutations.

---

## 1. Objective snapshot

| Item | Value |
|------|-------|
| Old public IP | `15.206.88.2` |
| Old EC2 public DNS | `ec2-15-206-88-2.ap-south-1.compute.amazonaws.com` → still resolves to `15.206.88.2` |
| New public IP | `3.110.50.174` |
| New EC2 public DNS | `ec2-3-110-50-174.ap-south-1.compute.amazonaws.com` → `3.110.50.174` |
| New private IP | `10.0.1.153` |
| New hostname | `ip-10-0-1-153` |
| SSH | `ubuntu@3.110.50.174` with **existing** PEM — **PASS** |
| Target portal hostname | `equip.iitr.ac.in` (preferred over raw IP) |

---

## 2. SSH / capacity verification (Phase 2) — PASS

| Check | Result |
|-------|--------|
| SSH with existing PEM | PASS |
| `nproc` | **8** |
| CPU | AMD EPYC 7571, 8 vCPU (4 cores × 2 threads), KVM |
| RAM | **30 GiB** total (~27 GiB available at sample) |
| Swap | **none** |
| Disk (`lsblk`) | `nvme0n1` **250G** |
| Root filesystem | `/dev/nvme0n1p1` ext4 **243G** total, **38G** used, **205G** free (**16%**) |
| Filesystem expansion needed? | **NO** — OS already sees full ~250 GB volume |
| GPU | **unavailable** (`nvidia-smi` missing; Amazon VGA only) |
| Uptime at check | ~1 hour since instance boot |
| Load average | `0.00–0.20` (idle) |

**Disk conclusion:** EBS + partition + ext4 already match the upgraded size. Do **not** run growpart/resize2fs unless a later check shows mismatch.

---

## 3. Resource baseline (Phase 3) — captured

### Docker services (running)

| Container | Status | Notes |
|-----------|--------|-------|
| `iic_booking_production_frontend` | healthy | `:8000` |
| `iic-booking-backend-django-1` | healthy | `:8080→5000` |
| `celeryworker` / `celerybeat` / `flower` | up | flower `:5555` |
| `redis` | healthy | internal |
| `reverse-tunnel-gateway` | healthy | **`:7090`** |
| `guacamole` / `guacd` / `guacamole-db` | healthy | guacamole `:8085` |

### Docker memory sample (idle)

Approx. aggregate app containers ≪ 3 GiB used vs 30 GiB host.  
Notable: Django ~545 MiB, Celery worker ~475 MiB, Guacamole ~484 MiB, Redis ~24 MiB.

### Host services of note

- Apache2 (TLS front for `equip.iitr.ac.in`)
- Docker / containerd
- MySQL (host) — separate from RDS Postgres used by Django
- GitHub Actions runners (backend + frontend) on this host
- CloudWatch agent
- **No Ollama process** observed

### GPU

`GPU = unavailable` — Copilot/Ollama on this host must be **CPU-only** if enabled later.

### Research Copilot

`RESEARCH_COPILOT_ENABLED` / settings probe: **false** (correct; leave OFF).

---

## 4. Network / DNS / health (Phase 4)

### DNS — **BLOCKER**

| Name | Resolves to | Verdict |
|------|-------------|---------|
| `equip.iitr.ac.in` A | **`15.206.88.2`** (TTL 3600) | **STALE — still old IP** |
| `ec2-15-206-88-2.ap-south-1.compute.amazonaws.com` | `15.206.88.2` | Old instance DNS (expected) |
| `ec2-3-110-50-174.ap-south-1.compute.amazonaws.com` | `3.110.50.174` | New instance DNS OK |

**Public `https://equip.iitr.ac.in`:** timed out from operator network (DNS still sends clients to old IP).

**Direct `http://3.110.50.174`:** HTTP **200** (new host responding).

**Ports on new IP (TCP):**

| Port | Result |
|------|--------|
| 22 | open (SSH works) |
| 443 | open |
| 7090 | open (reverse tunnel) |

### Local health on new host (bypass DNS)

| Endpoint | Result |
|----------|--------|
| `http://127.0.0.1:8080/api/version` | **200** JSON (`backend_version` 2.5.2) |
| `http://127.0.0.1:8080/api/v1/analysis/health/live/` | **ok** |
| `http://127.0.0.1:8080/api/v1/analysis/health/ready/` | **ready** (DB/cache/tunnel/guacamole ok) |
| `http://127.0.0.1:8000/` | **200** |
| Outbound HTTPS | google **200** |

Apache vhost `equip.iitr.ac.in.conf` proxies:

- `/api/` → `127.0.0.1:8080`
- `/guacamole/` → `127.0.0.1:8085`
- `/` → `127.0.0.1:8000`

(No hard-coded old IP in Apache site config.)

### Security groups / IAM / EIP

**Not verified via AWS API in this pass** (no AWS CLI credentials used from Cursor).  
**Required before declaring PASS:** confirm SG allows 22/80/443/7090 as currently used; confirm no accidental public exposure of Postgres/Redis/Ollama; confirm whether Elastic IP was reassociated or DNS must change instead.

---

## 5. Inventory of `15.206.88.2` / related references (Phase 1)

### Classification key

- **A** Active production config  
- **B** Active deploy/automation  
- **C** Active application config  
- **D** Active DSA/RAA config  
- **E** Active monitoring/health  
- **F** Docs describing current config  
- **G** Historical release/log/incident  
- **H** Test/fixture/example  
- **I** Unknown  

### ACTIVE — update required (A–D)

| Location | Reference | Class | Proposed action |
|----------|-----------|-------|-----------------|
| **Live EC2 env** `DJANGO_ALLOWED_HOSTS` | contains `15.206.88.2` | **A/C** | Add `3.110.50.174`; keep hostname `equip.iitr.ac.in`; remove old IP after cutover settles |
| **Live EC2 env** `RA_TUNNEL_GATEWAY_WSS_URL` | `ws://ec2-15-206-88-2.ap-south-1.compute.amazonaws.com:7090/tunnel` | **A/D** | **CRITICAL** — agents cannot join reverse tunnel until this points at new host. Prefer `ws://ec2-3-110-50-174.ap-south-1.compute.amazonaws.com:7090/tunnel` or (better long-term) `wss://equip.iitr.ac.in:7090/tunnel` **only if** TLS/proxy for 7090 is designed. Interim safe: new EC2 public DNS or `ws://3.110.50.174:7090/tunnel` |
| **Live DB** `RemoteAnalysisSettings.tunnel_gateway_wss_url` | same old EC2 hostname | **A/D** | Update in lockstep with env (or confirm env overlay wins) |
| **GitHub secret** `EC2_HOST` (workflows use `secrets.EC2_HOST`) | presumed old IP (not in git) | **B** | Set to `3.110.50.174` in GitHub repo secrets (backend + frontend if separate) |
| `config/settings/production.py` default `ALLOWED_HOSTS` (backend / deploy / ai17 / rt-port trees) | default list includes `15.206.88.2` | **C** | Change default to `3.110.50.174` **or** remove IP default and rely on env + hostname only |
| Comment/example CORS line in same file | `http://15.206.88.2` | **H/F** | Update example IP in comment when touching file (optional) |

### Prefer hostname — do **not** replace with raw IP

| Area | Current | Class | Action |
|------|---------|-------|--------|
| RAA installer / `PortalBaseUrl` | `https://equip.iitr.ac.in` | **D** | **Keep hostname** |
| DSA installer portal default | `https://equip.iitr.ac.in` | **D** | **Keep hostname** |
| Android release API | `https://equip.iitr.ac.in/api/` | **C** | **Keep hostname** |
| Frontend / `FRONTEND_URL` | `https://equip.iitr.ac.in` | **C** | **Keep hostname** |
| Guacamole public base | `https://equip.iitr.ac.in/guacamole` | **A** | **Keep hostname** (works after DNS cutover) |
| Compose internal | `redis`, `guacamole`, `reverse-tunnel-gateway` | **C** | **Keep service names** |

### Historical / retain (G)

| Location | Notes |
|----------|-------|
| `RemoteAnalysisAgent/docs/release/production/Production-Deployment-Report.md` | Mentions `ec2-15-206-88-2…` as past environment — **retain** |
| Numerous `docs/release/phase-*` reports under backend trees | Historical qualification — **retain** |
| Operator chat / ad-hoc `tmp_*.py` scripts that SSH’d to old IP | Local ops debris — update only if still used |

### Not found as hard-coded IP

- `iic-booking-frontend` source: no `15.206.88.2`
- `iic-booking-android` source: uses hostname
- `DepartmentSyncAgent` / `RemoteAnalysisAgent` app code: portal hostname, not old IP
- `ReverseTunnelGateway` repo: no old IP hits

### Cursor / operator habit

Prior Cursor sessions used:

`ubuntu@ec2-15-206-88-2.ap-south-1.compute.amazonaws.com`

→ Must switch ops to `ubuntu@3.110.50.174` (or new public DNS). PEM unchanged.

---

## 6. Critical blockers (do not declare migration PASS yet)

1. **DNS A record for `equip.iitr.ac.in` still → `15.206.88.2`**  
   Until IITR DNS (or Route53/registrar) is updated to `3.110.50.174` (or an EIP/LB in front of the new instance), public portal traffic will miss the new host.

2. **Reverse tunnel WSS URL still names the old EC2 hostname**  
   `RA_TUNNEL_GATEWAY_WSS_URL` / DB setting → `ec2-15-206-88-2…:7090`  
   RAA JOIN_TUNNEL will target the wrong machine even after DNS for equip is fixed (unless that hostname is somehow re-pointed — it currently resolves to the old IP).

3. **GitHub `EC2_HOST` secret** must be verified/updated or CD will SSH to the wrong host.

4. **AWS SG / EIP association** not yet audited via AWS console/CLI in this pass.

5. **Public portal health via hostname** cannot PASS until DNS moves.

---

## 7. Ollama / Copilot suitability (qualification only — no install)

| Factor | Observation |
|--------|-------------|
| Host RAM headroom | ~27 GiB available at idle |
| CPU | 8 vCPU, load ~0 idle |
| GPU | none |
| Current app footprint | Comfortable (&lt; ~3–4 GiB containers + host MySQL/CW) |
| Recommendation (preliminary) | Prefer **`llama3.2:1b`** for pilot; **`llama3.2:3b`** only with hard CPU/memory limits + low concurrency |
| Do not install in this phase | Confirmed — inventory only |
| Keep | `RESEARCH_COPILOT_ENABLED=false` |

Risk: CPU inference on same box as Django/Celery/Guacamole can starve workers under load — enforce cgroup limits if later installed; never publish `:11434` publicly.

---

## 8. Proposed next actions (await operator approval — Phase 5+)

**Order matters:**

1. **DNS:** Change `equip.iitr.ac.in` A → `3.110.50.174` (or attach Elastic IP / update EIP association). Document who owns DNS.  
2. **Tunnel WSS:** Update env + DB `tunnel_gateway_wss_url` to new EC2 public DNS or IP `:7090/tunnel`; restart django (and confirm agents can JOIN). Prefer later: terminate TLS via Apache and use `wss://equip.iitr.ac.in/...` without redesigning silently.  
3. **ALLOWED_HOSTS:** Add `3.110.50.174`; optionally drop old IP after soak.  
4. **GitHub secret `EC2_HOST`:** → `3.110.50.174`.  
5. **Repo defaults** in `production.py` (canonical deploy tree): replace default old IP.  
6. **Re-test:** `https://equip.iitr.ac.in` version/live/ready, login, booking smoke, RAA heartbeat, reverse tunnel join.  
7. **Only then** continue DSA/RAA runtime evidence and final acceptance checklist.

---

## 9. Acceptance checklist (current)

| Criterion | Status |
|-----------|--------|
| SSH works with existing PEM | PASS |
| New IP reachable | PASS |
| 8 vCPU verified | PASS |
| ~32 GB RAM verified | PASS (~30 GiB) |
| ~250 GB storage verified | PASS |
| Filesystem sees capacity | PASS (no resize needed) |
| DNS verified | **FAIL** (still old IP) |
| Security groups verified | **NOT DONE** |
| Active old-IP audit | PASS (inventory done) |
| Required active refs updated | **NOT STARTED** (awaiting approval) |
| Historical refs preserved | PASS (none rewritten) |
| Production health via hostname | **FAIL** until DNS |
| Local API/ready on new host | PASS |
| RAA tunnel URL correct | **FAIL** (still old EC2 hostname) |
| Research Copilot OFF | PASS |
| Ollama not installed this task | PASS |

---

## 10. Explicit non-actions taken

- No DNS changes  
- No env/DB writes  
- No docker restarts for migration  
- No Ollama install  
- No `RESEARCH_COPILOT_ENABLED=true`  
- No blind global search-replace  
- No force-push / unrelated branch edits  

---

**STOP POINT:** Ready for operator decision on DNS ownership + approval to proceed with Phase 5 (active IP/config updates) on the new host only.
