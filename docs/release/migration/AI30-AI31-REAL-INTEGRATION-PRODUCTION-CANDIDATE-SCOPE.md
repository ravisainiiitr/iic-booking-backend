# REAL Integration — Production Candidate Scope

**Branch:** `release/real-integration-production-candidate`  
**Implementation tip (cherry-picked freeze docs):** `866eb174ba5740d575b38ddcaaeb08611043cb7d`  
**Branch tip:** this docs commit (run `git rev-parse HEAD` on the branch)  
**Base:** `origin/master` (`f8c0892`)  
**Date (UTC):** 2026-08-21  

| Gate | Status |
|------|--------|
| PRODUCTION DEPLOYMENT | **NO** |
| PRODUCTION MIGRATION | **NO** |
| PRODUCTION WRITES | **NO** |
| Push / GitHub PR | **NOT DONE** (local only) |
| PR #86 | **UNTOUCHED** (still OPEN) |

---

## 1. Why PR #86 was not selected as-is

PR #86 (`release/real-integration-staging-qualified`) contains **9 commits / ~141 files**, including:

- R14 auto-complete / RA data-selection (`4e7f887`…`0ed6032`)
- migration **`equipment.0188`**
- Channel-I identity + wallet credit v2
- REAL integration freeze (`bd71757` + `d600f25`)

The final scope audit (`AI30-AI31-PR86-FINAL-SCOPE-AUDIT.md`) recommended **OPTION B**: ship identity + REAL without R14.

---

## 2. Why R14 / 0188 are excluded

- R14 is **not required** by REAL preflight, activation, or live Channel-I evidence.
- REAL modules do **not** import R14 auto-complete / data-selection APIs.
- `0188` only adds equipment auto-complete / data-selection fields (defaults OFF) and expands production schema risk without improving REAL gates.

**R14 commits excluded:**

| SHA | Subject |
|-----|---------|
| `4e7f887` | feat(R14): auto-complete + data selection |
| `dc709cb` | docs(R14.1) |
| `9254300` | docs(R14.2) |
| `085d023` | test(R14.2) dummy booking E2E |
| `0ed6032` | docs(R14.3) |

**Confirmed absent on this branch:** `0188`, `docs/release/phase-R14/**`, `test_r14_*`.

---

## 3. Why 0096–0100 are retained

Historical note: on PR #86, `747f514`’s **Git parent** was R14 tip `0ed6032`. That is **historical stacking**, not a REAL code dependency.

REAL live evidence loads `ChannelIIdentityProfile` (created in **`users.0099`**). On this branch the migration chain is linear:

```text
0095 → 0096 → 0097 → 0098 → 0099 → 0100
```

| Migration | Why retained |
|-----------|--------------|
| 0096 | Portal migration / legacy ledger models (chain) |
| 0097 | Observability (chain) |
| 0098 | Wallet credit facility v2 schema (chain predecessor of 0099; not imported by REAL code) |
| 0099 | **Required** — `ChannelIIdentityProfile` |
| 0100 | Gender / enrolment fields used by identity claim mapping |

**Migrations have NOT been applied to production.** Presence in the branch ≠ applied.

---

## 4. Required REAL integration components

Present on branch:

- `iic_booking/users/legacy_ledger/real_integration_guards.py`
- `iic_booking/users/legacy_ledger/real_integration_activation.py`
- `iic_booking/users/legacy_ledger/real_integration_live_evidence.py`
- `iic_booking/users/legacy_ledger/snapshot_reader.py`
- `iic_booking/users/identity/channel_i_fixture.py`
- Commands: `real_integration_status`, `real_integration_preflight`, `real_integration_activate_staging`
- Tests: `test_real_integration_preflight`, `test_real_integration_activation`
- Staging settings / compose / sample.env.staging
- Production hard-OFF block in `config/settings/production.py`

---

## 5. Staging qualification result (unchanged evidence)

| Gate | Result |
|------|--------|
| Channel-I REAL | PASS |
| Employee claim `username` | PASS |
| `username` → `admin.users.emp_id` exact 1 | PASS |
| MySQL RO / wallet / ledger / booking | PASS |
| Fixture isolation | PASS |
| REAL tests | **30 PASS** (reconfirmed on this branch) |
| S3 | **NOT_AVAILABLE** (staging limitation only — not PASS) |

---

## 6. Production hard-off

Forced in `config/settings/production.py`:

```text
DEPLOYMENT_ENVIRONMENT = "PRODUCTION"
REAL_INTEGRATION_ENABLED = False
CHANNEL_I_STAGING_FIXTURE_MODE = False
LEGACY_MYSQL_STAGING_FIXTURE_MODE = False
LOCAL_STAGING_ACCEPTED = False
```

Fixture loaders raise `ImproperlyConfigured` under production / REAL intent. No silent fallback to fixtures, staging snapshot, or `LOCAL_STAGING_ACCEPTED` in production settings.

---

## 7. Migration list (this branch)

| Present | Absent |
|---------|--------|
| `users.0096` … `users.0100` | `equipment.0188` |

**Production migration = NOT RUN.**

---

## 8. Test results

| Suite | Result |
|-------|--------|
| REAL preflight + activation | **30 PASS** (worktree code bind-mounted into staging image; isolated from production) |
| Channel-I architecture / wallet v2 / portal ledger | **Not re-executed to green** in this audit: shared staging DB migrate conflict / SQLite incompatible with PG-only migration SQL. Suites **are present** in the branch. Do not treat as REAL regression. |

No production write tests were run.

---

## 9. Production status

```text
PRODUCTION DEPLOYMENT = NO
PRODUCTION MIGRATION = NO
PRODUCTION WRITES = NO
READY FOR HUMAN PUSH REVIEW = YES (local branch only)
```

---

## 10. How this branch was built

1. Clean worktree from `origin/master` (main dirty tree preserved).
2. Cherry-pick intended stack in order.
3. On `747f514` cherry-pick: **modify/delete conflicts** only on incidental R14 docs that do not exist on `master`. Resolved by **removing** those R14 docs (intentional OPTION B exclusion). `service.py` auto-merged (RDP credential fail-fast; not R14 schema).
4. Remaining cherry-picks (`f7783f9`, `bd71757`, `d600f25`) applied cleanly.

### Commit mapping (original → this branch)

| Original (PR #86 lineage) | This branch | Notes |
|---------------------------|-------------|-------|
| `747f514` | `c476cf4` | Same feature; R14 doc hunks dropped |
| `f7783f9` | `2a21c02` | Equivalent |
| `bd71757` | `f79b130` | Equivalent freeze implementation |
| `d600f25` | `866eb17` | Equivalent freeze SHA doc (references original freeze SHA in release doc) |
| (scope doc) | tip | This file |

**PR #86 branch `release/real-integration-staging-qualified` was not modified.**

---

## 11. Secret audit

| Check | Result |
|-------|--------|
| `.envs/.staging/.django` / `.envs/.production/.django` tracked | **NO** |
| OAuth tokens / AWS keys / SSH private keys committed | **PASS** (none found as committed secrets) |

---

## 12. Next human steps (only after push approval)

1. Review this document + `git log` / `git diff master...HEAD`
2. Push `release/real-integration-production-candidate`
3. Open new PR (do **not** merge #86 for this release path)
4. Review / merge → capture **actual merge SHA**
5. Production candidate deploy + **read-only** qualification
6. Separate approval still required for any production `migrate`
