# Release Checklist — Platform 2.5.0-rc1

Check items only with evidence (link, log path, or SHA). **Commits are not started until this checklist is approved in principle and the Readiness Report is accepted.**

## A. Repository hygiene

- [ ] Working trees free of junk (`tmp_commission_run.py`, `artifacts/dsa-installer`, `*.db`, local secrets)
- [ ] No detached HEAD for DSA at commit time
- [ ] RAA has initial git history
- [ ] `.gitignore` covers `artifacts/`, `bin/`, `obj/`, `node_modules/`, `data/*.db*`
- [ ] Release branch strategy approved (`release/phase-2.5` per prior prep report)
- [ ] Logical commit groups reviewed ([Release-Preparation-Report](../../phase-2.5/Release-Preparation-Report-2026-08-04.md))

## B. Version control / traceability

- [ ] All RC commits created on release branch (approval-gated)
- [ ] Tags created per [Versioning Strategy](./03-Versioning-Strategy.md)
- [ ] [Release Manifest](./00-Release-Manifest.md) SHAs filled (no TBD for commits)
- [ ] CI builds triggered from tags only for RC artifacts

## C. Quality gates

- [ ] Backend unit/integration tests green on release SHA
- [ ] Reverse tunnel tests green
- [ ] Lab infrastructure smoke tests green
- [ ] Frontend production build succeeds (`npm ci && npm run build`)
- [ ] DSA `Publish-DsaInstaller.ps1 -Version 1.0.0-rc1` succeeds on clean agent
- [ ] RAA publish script (to be added) succeeds
- [ ] Wizard publish succeeds
- [ ] **Lab SAT** Stage 1–5 executed; evidence in Test Dashboard
- [ ] Critical defects = 0
- [ ] High defects = 0 (or formally waived with sign-off)
- [ ] Security plan subset executed
- [ ] Performance baselines recorded or waived

## D. Data / migrations

- [ ] Staging migrate dry-run from pre-2.5 backup
- [ ] Migration order verified (equipment → RA → sync → deployment → lab)
- [ ] RA `0017_restore` verified on staging dump (empty `0015` stub hosts)
- [ ] lab `0002`/`0003` present with matching models
- [ ] Rollback/restore drill documented ([Rollback Plan](./05-Rollback-Plan.md))

## E. Containers / deploy

- [ ] Backend image built from tag; digest recorded
- [ ] Frontend image built with correct `VITE_API_URL`; digest recorded
- [ ] `collectstatic` / nginx assets verified
- [ ] Celery worker + beat healthy; lab detectors scheduled
- [ ] Guacamole + reverse tunnel config validated
- [ ] Smoke: Main Admin sees Lab / Deployment Center / SAT Dashboard
- [ ] Smoke: `GET /api/v1/lab/infrastructure/` and `/api/v1/deployment/` authenticated OK

## F. Installers / Deployment Center

- [ ] DSA/RAA/Wizard uploaded with SHA-256
- [ ] Compatibility matrix JSON filled
- [ ] Signature status recorded (or explicit “unsigned RC” waiver)
- [ ] Silent install / upgrade / repair paths smoke-tested ([Installer Validation](./07-Installer-Validation.md))

## G. Documentation

- [ ] Release Notes finalized
- [ ] Known Issues finalized
- [ ] Upgrade + Rollback guides reviewed by ops
- [ ] Administrator / Lab SAT guides linked
- [ ] Manifest complete

## H. Go decision

- [ ] RC1 Readiness Report = **GO** or approved **Conditional GO** with dated waivers
- [ ] Explicit written approval to merge `release/phase-2.5` → `master`
- [ ] Explicit written approval to push / deploy production

**Rule:** Do not consider commits “done” for production until A–G are checked and H is signed.
