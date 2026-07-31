# Go-Live Checklist — Remote Analysis Platform

Mark each item PASS before go-live. Capture CommissioningRunId + evidence ZIP.

## Pre-flight

- [ ] Portal migrations applied (incl. reverse tunnel + commissioning)
- [ ] `transport_mode=reverse_tunnel` on production settings
- [ ] Tunnel gateway healthy (Live Commissioning GREEN/AMBER acceptable only if explained)
- [ ] Guacamole production (not mock) configured
- [ ] At least one Analysis PC agent online with heartbeat ≤ 90s
- [ ] DSA / RawData path validated for department equipment
- [ ] Outside-IIT researcher test account ready

## Exit path (single pass)

- [ ] Login outside IIT
- [ ] Open completed booking → Analyze Data
- [ ] Allocate workstation
- [ ] Workspace prepared
- [ ] Reverse tunnel established
- [ ] Browser desktop opens
- [ ] Analysis software operable
- [ ] Results saved + uploaded
- [ ] Cleanup + workstation released
- [ ] Second researcher allocates successfully
- [ ] Evidence ZIP archived

**Sign-off:** _______________ **Date:** _______________
