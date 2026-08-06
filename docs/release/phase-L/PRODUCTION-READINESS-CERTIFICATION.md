# PRODUCTION READINESS CERTIFICATION

**Programme:** IIC Equipment Booking Portal — Phase L Final Production Integration & Go-Live Qualification  
**Date:** 2026-08-06  
**Production URL:** https://equip.iitr.ac.in  
**Qualified backend release:** `v2.5.0-rc24-release` (`b3bf95c`)  
**Suggested Final version:** **v2.5.0 Final**

---

## Overall completion

| Phase | Result |
|-------|--------|
| L1 Department Sync Agent | PASS |
| L2 Remote Analysis | PASS |
| L3 Laboratory Workflow (internal + external) | PASS (after rc24) |
| L4 Performance | PASS |
| L5 Security | PASS |
| L6 Operational | PASS with WARN |
| L7 Documentation | PASS |
| L8 Production Readiness | CONDITIONAL GO |

**Overall completion: ~96%**  
(Remaining: destructive reboot drill deferred; multi-agent/multi-RA concurrent N/A; automated backup cron not yet live.)

---

## Go-Live recommendation

**GO — institute-wide rollout approved with conditions.**

Conditions (complete within 7 days of go-live):

1. Assign owner for **nightly managed-DB / PostgreSQL backups** and one restore test.  
2. Reduce EC2 root disk usage below **70%**.  
3. Track frontend Docker healthcheck false-negative as P3 ops ticket.

---

## Residual risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Disk 80% full | Medium | Prune images/logs; expand volume |
| Backup automation incomplete | Medium | Schedule off-box dumps/snapshots |
| Single Analysis PC / single DSA | Medium | Capacity planning; queue expected |
| Frontend healthcheck unhealthy | Low | Add `/health`; site already serves |
| Orphan RA RESERVED without reservation | Low | Admin CLEAN_WORKSTATION (verified) |
| I-STEM FBR gate for external downloads | Info | By design — communicate to externals |

---

## Post–Go-Live monitoring

- Agent heartbeats (DSA + RAA) age &lt; 60–90 s  
- Django/Celery container health + Flower queue depth  
- Guacamole + reverse-tunnel gateway health  
- S3 result upload success / DSA UploadQueue failures  
- Disk free space; Redis memory; API p95 for bookings/slots  
- Error rate on login, book, sample-trace, analysis/start  

---

## Certification statement

Backend development for this release train is **COMPLETE**. Phase L integration qualification is **PASSED** with conditional operational follow-ups. No open production code blockers remain after `v2.5.0-rc24-release`.

**Signed (system):** Phase L automated qualification · 2026-08-06  
**Suggested cut:** promote `v2.5.0-rc24-release` → **v2.5.0 Final**
