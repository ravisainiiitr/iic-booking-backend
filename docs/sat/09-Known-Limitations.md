# 09 — Known Limitations

Documented constraints for SAT (not defects unless they violate Pass/Fail unexpectedly).

1. **Guacamole vs sync path** — Commissioning SAT primary path does not require Guacamole. Desktop session phases (`SessionStarting` / `SessionActive`) may be N/A when validating file-sync-only.
2. **Portal SPA login** — API login issues DRF Token and may not set Django session; HTML console uses login redirect and optional `?token=` handoff.
3. **Query token** — `?token=` authenticates **HTML GET only**, then stripped; JSON APIs require header/session.
4. **Large files** — >1 GB depends on reverse-proxy timeouts, client body size, and disk; may need infra tuning (not app feature work during SAT).
5. **Enrollment key** — Required when configured / non-DEBUG; local DEBUG may allow open register (see hardening tests).
6. **Range/resume** — Interrupted transfer behavior may be fail+retry rather than byte-range resume; SAT verifies safe failure and successful retry.
7. **Redis** — Optional depending on deployment; mark SAT-06.03 N/A if unused.
8. **Single-agent lab** — SAT-08.04 (20 workstations) may use simulated heartbeats from harness if 20 PCs unavailable — note method.
9. **Checksum** — Authority is portal-stored SHA-256; agent must not mark success if verify fails.
10. **Clock skew** — Token expiry and heartbeat age assume reasonable NTP; >5 minutes skew can false-offline.

Update this list when SAT discovers permanent product limits; file defects for unexpected gaps.
