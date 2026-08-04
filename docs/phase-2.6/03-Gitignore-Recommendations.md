# .gitignore Recommendations — Phase 2.6

**Do not modify `.gitignore` files in this phase.** Apply only when repository recovery commits are approved.

---

## Portal Backend — add / verify

```gitignore
# Local ad-hoc
tmp_*.py
tmp/
*.local.md

# Release exports (never commit)
artifacts/

# Env secrets (verify already ignored)
.envs/.production/
.envs/.local/
!.envs/.local/.gitkeep
```

Confirm existing: `__pycache__/`, `.venv/`, `media/`, static collected paths per project norms.

---

## Portal Frontend — add / verify

```gitignore
# Already typical
node_modules/
dist/
*.local

# Optional
artifacts/
coverage/
playwright-report/
test-results/
```

---

## DSA — **critical gap**

Current ignores `Backend/artifacts/` and `publish/` but **not** top-level `artifacts/` (where ~383 installer outputs live).

```gitignore
# Add
artifacts/
**/artifacts/

# Strengthen (if not present)
*.exe
*.zip
*.pdb
*.dll
!**/Payload/.gitkeep
# Prefer ignoring built Payload contents, keep placeholders only
Backend/src/DepartmentSyncAgent.Installer/Payload/**
!Backend/src/DepartmentSyncAgent.Installer/Payload/.gitignore
!Backend/src/DepartmentSyncAgent.Installer/Payload/.gitkeep
```

Keep: `**/bin/`, `**/obj/`, `**/node_modules/`, `**/dist/`, `.vs/`, `logs/`.

---

## RAA — verify enforcement

Already lists `bin/`, `obj/`, `data/*.db`, `data/*.db-*`. Ensure:

```gitignore
artifacts/
*.db
*.db-shm
*.db-wal
tmp-*.txt
TestResults/
```

Confirm `git status` no longer lists `data/RemoteAnalysis.db*` after ignore refresh (may need `git check-ignore -v`).

---

## Equipment Wizard

Inherits DSA ignore file — no separate repo. Same `bin/`/`obj/` rules apply.

---

## Policy

| Allowed in git | Never in git |
|----------------|--------------|
| Source, tests, docs, scripts, lockfiles | Installers, zips, DLLs, PDBs, node_modules, dist, local DBs, logs, `.env` secrets, IDE caches |
