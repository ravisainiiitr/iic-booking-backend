# AI.10 — Limited Production Pilot Checklist

Use before expanding the pilot cohort. Mark only with evidence.

## Platform

- [ ] Backend healthy (`/api/v1/analysis/health/ready/` → 200)
- [ ] Frontend healthy (portal home → 200)
- [ ] `/api/version/` → 200
- [ ] `/api/v1/provisioning/capabilities/` → 200 and `research_copilot=false`
- [ ] Migrations verified (deploy migrate on start / `migrate-production` / `showmigrations` on host)
- [ ] Monitoring checked (container health, disk, celery/redis; Sentry if configured)
- [ ] Backup status documented (nightly job path known; live restore **NOT** required for checklist)
- [ ] Rollback documented (previous tag / `scripts/deploy/rollback.sh`)

## Core booking

- [ ] Booking verified (controlled account)
- [ ] Cancellation verified (policy-aware)
- [ ] Sample acceptance verified
- [ ] Sample rejection verified (**reason required**)
- [ ] Completion verified
- [ ] Result workflow verified (list + download)
- [ ] Email without attachment verified
- [ ] Notification deep links verified (Alerts → Booking Detail)
- [ ] Cross-user result access denied (403)

## Android

- [ ] Android release verified (`assembleRelease`; API = `https://equip.iitr.ac.in/api/`)
- [ ] Release has no debug API base (`10.0.2.2` absent from release APK config)
- [ ] Login / Home / Bookings / Detail / Results / Alerts / Profile / Logout
- [ ] Force-stop reopen keeps session
- [ ] 401 clears session → Sign in
- [ ] Operator Operations: accept / reject / complete path known

## Lab edge

- [ ] DSA verified (service + portal online)
- [ ] Equipment PC verified (LAN only; no Internet dependency introduced)
- [ ] RAA verified (heartbeat) **or** explicitly N/A for this cohort
- [ ] Remote Analysis verified for software-centric path **or** marked PARTIAL / NOT QUALIFIED

## Feature flags

- [ ] Copilot OFF (`RESEARCH_COPILOT_ENABLED=false`; capabilities false)
- [ ] FCM OFF (no production FCM enablement for this pilot)

## Sign-off

| Role | Name | Date | Notes |
|------|------|------|-------|
| Portal Admin | | | |
| Lab Owner | | | |
| Mobile Owner | | | |

**AI.10 automated evidence (2026-08-11):** production health endpoints 200; capabilities `research_copilot=false`; backend regression 34 passed; Android release build PASS; frontend `npm run build` PASS; emulator smoke Home/Bookings/Alerts/Profile + force-stop persist PASS (debug→local). Live S3 E2E and FCM remain blocked by credentials. Production migration host `showmigrations` not executed from this workstation → verify on EC2 before cohort expansion.
