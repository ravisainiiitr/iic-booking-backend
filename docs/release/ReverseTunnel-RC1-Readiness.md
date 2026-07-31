# Reverse Tunnel RC1 Readiness Report

**Platform version:** `1.0.0-RT-RC1`  
**Prepared:** 2026-07-31  
**Scope:** Release engineering only — no commit / push / tag / deploy executed in this pass.

---

## 1. Architecture (unchanged)

```
Researcher Browser
  → AWS Booking Portal (Django)
  → Guacamole / guacd
  → Reverse Tunnel Gateway (adapter TCP + agent WSS)   [when reverse_tunnel]
  → Windows Remote Analysis Agent
  → Analysis Workstation RDP :3389
```

Additive transport: `direct_rdp` (default) | `reverse_tunnel` (commissioning / go-live later).  
**RC1 first production ship keeps `RA_TRANSPORT=direct_rdp`.**

---

## 2. Repository state summary (Step 1)

| Repository | Branch | HEAD | Working tree | Modified | Untracked | Build | Tests | Version today |
|------------|--------|------|--------------|----------|-----------|-------|-------|---------------|
| **iic-booking-backend** (Portal) | `main` → `origin/master` | `ac70cfa61deb3554d4932be461db3ef77a5ea0c9` | Dirty (RC1 + unrelated) | 24 | 37 | compile OK | **23 passed** (RT release suite) | planned `1.0.0-RT-RC1` |
| **ReverseTunnelGateway** | — | **N/A (not a git repo)** | Source present | — | — | Release build OK | **2 passed** | planned `1.0.0-RT-RC1` |
| **RemoteAnalysisAgent** | `master` (no upstream shown) | `3c48c16c4f2ef60a08e36dc63aad17810e371acd` | Dirty (tunnel + other) | 13 | 21 | Release build OK | **20 passed** | csproj `1.0.0` → align to `1.0.0-RT-RC1` |

---

## 3. Versioning strategy (Step 2)

| Layer | Version |
|-------|---------|
| Remote Analysis Platform | **`1.0.0-RT-RC1`** |
| Portal | `1.0.0-RT-RC1` |
| Gateway | `1.0.0-RT-RC1` |
| Agent | `1.0.0-RT-RC1` |
| Migration | **`0015`** (`remote_analysis.0015_reverse_tunnel_transport`) |
| Docker tags (recommended) | `iic_booking_production_django:1.0.0-RT-RC1`, `reverse-tunnel-gateway:1.0.0-RT-RC1` |

Do **not** create git tags until explicitly ordered after commits.

---

## 4. Component readiness

### Portal

- RC1 include set gated **READY FOR RC1 COMMIT** (prior audit).  
- Unrelated files must stay unstaged (desktop CSRF, local CSRF, reservation window, reports).  
- Migration `0015`, tunnel core, compose gateway service, toolkit/commissioning, docs present (uncommitted).

### Gateway

- Builds and framing tests pass.  
- **Critical prep gap:** directory is **not a git repository** — cannot pin `_GATEWAY_RC1_SHA_` until `git init` + first commit (operator action; not done here).  
- Compose expects sibling path `../ReverseTunnelGateway`.

### Agent

- Builds; **20** tests pass including tunnel frame tests.  
- Tree is dirty; RT files under `src/.../Tunnel/` untracked — needs curated Agent RC1 commit (exclude unrelated if any).  
- Version in csproj currently `1.0.0`.

### Database

- Migration chain verified previously; `0015` depends on `0014` + `equipment.0181`.  
- Reversible schema ops (no irreversible RunPython).

### Docker

- Portal Dockerfile unchanged; gateway Dockerfile present.  
- `docker-compose.ra-production.yml` adds `reverse-tunnel-gateway` (uncommitted).

### Testing (this pass)

| Suite | Result |
|-------|--------|
| Portal RT release pytest | **23 passed** |
| Gateway `dotnet test` | **2 passed** (NU1510 warning) |
| Agent `dotnet test` | **20 passed** |

### Documentation

| Doc | Path |
|-----|------|
| Compatibility matrix | `docs/release/CompatibilityMatrix.md` |
| Release checklist | `docs/release/ReleaseChecklist.md` |
| Live commissioning | `docs/release/LiveCommissioningChecklist.md` |
| BOM / Manifest / Deploy steps | existing under `docs/release/` and `docs/deploy/` |

---

## 5. Deployment order (Step 6) — with rollback

| Step | Action | Keep transport | Rollback |
|------|--------|----------------|----------|
| 1 | Commit Portal RC1 (include list only) on `release/reverse-tunnel-rc1` | — | Delete branch / reset (unpushed) |
| 2 | Init git + commit Gateway RC1 | — | Discard Gateway commit |
| 3 | Commit Agent RC1 (tunnel-capable) | — | Reset Agent branch |
| 4 | Fill CompatibilityMatrix SHAs | — | Docs only |
| 5 | Push Portal (when ordered) | — | Revert PR / redeploy prior SHA |
| 6 | Push Gateway (when ordered) | — | Revert |
| 7 | Push Agent (when ordered) | — | Revert |
| 8 | Build Docker images on host | — | Do not restart yet |
| 9 | Deploy Portal containers | `direct_rdp` | Prior image + `rollback.sh` |
| 10 | Run migration `0015` | `direct_rdp` | Restore DB backup |
| 11 | Deploy Gateway container | `direct_rdp` | `stop reverse-tunnel-gateway` |
| 12 | Upgrade Agent on Analysis PC | `direct_rdp` | Reinstall prior Agent build |
| 13 | Verify health (Portal live, gateway, heartbeat) | **`RA_TRANSPORT=direct_rdp`** | Prior stack |
| 14 | Complete Live Commissioning checklist | flip to `reverse_tunnel` only for tunnel rows if approved | Flip flag back to `direct_rdp`; stop gateway if needed |
| 15 | Enable `reverse_tunnel` for users **only after** checklist PASS | `reverse_tunnel` | Set `direct_rdp` + sync settings; stop/idle gateway |

**Never skip step 13→14→15 order.**

---

## 6. Rollback (summary)

1. Set `RA_TRANSPORT=direct_rdp`; sync settings.  
2. Stop Gateway.  
3. Portal: prior SHA / `scripts/deploy/rollback.sh`.  
4. DB: restore pre-`0015` backup if schema must unwind.  
5. Agent: prior Windows build.  
6. Confirm Portal liveness + booking site.

---

## 7. Commissioning

Use `docs/release/LiveCommissioningChecklist.md`. Archive evidence ZIP per run. Defect workflow: stop → evidence → minimal fix → regression → re-commission.

---

## 8. Outstanding risks

| Rank | Risk | Mitigation |
|------|------|------------|
| Critical | Gateway not a git repo — no SHA pin | Operator `git init` + RC1 commit before push/deploy pin |
| High | Agent working tree mixed — risk of non-RT files in Agent RC1 | Curate Agent include list like Portal |
| High | Production Guacamole readiness historically mock-related 503 | Fix Guacamole config separately; do not conflate with transport enable |
| Medium | Compose sibling path easy to miss on host | Documented in deploy steps / BOM |
| Medium | Unrelated Portal diffs still in working tree | Must not stage into Portal RC1 |
| Low | Gateway NU1510 warning | Non-blocking |
| Low | No git tags yet | Intentional until ordered |

---

## 9. Recommendations

1. Commit **Portal RC1** first using the approved include list.  
2. Initialize **Gateway** git and commit `1.0.0-RT-RC1`.  
3. Curate and commit **Agent** tunnel RC1; set version string to `1.0.0-RT-RC1`.  
4. Update CompatibilityMatrix with three SHAs.  
5. Deploy with **`direct_rdp`**; gateway idle OK.  
6. Run Live Commissioning before any user-facing `reverse_tunnel`.

---

## 10. Final verdict

# READY TO COMMIT RC1

**Meaning:** Portal RC1 content and synchronized release artifacts are ready for the **Portal commit** (when you explicitly order it).  

**Before claiming a full three-repo synchronized commit is complete:** initialize Gateway as git and curate Agent RC1 commit, then pin all three SHAs in `CompatibilityMatrix.md`.

No push, tag, or production deploy until separately ordered.
