# Code Cleanup Report — Remote Analysis

**Date:** 2026-07-30  
**Scope:** `iic_booking/remote_analysis`, `RemoteAnalysisAgent`, related docs  
**Policy:** Do not remove intentionally retained compatibility, feature flags, or test fixtures.

---

## Summary

| Category | Count (approx.) | Action |
|----------|-----------------|--------|
| Must-fix before production | 3 | Ops / config gates |
| Intentional retained | Many | Keep |
| Test-only | Tests + fixtures | Keep |
| False positives | `pass` in except, empty Exception bodies | Ignore |
| Dead / obsolete APIs/models | 0 found | — |

---

## Must-fix / must-configure before production

| Item | Location | Notes |
|------|----------|-------|
| `mock_guacamole=True` default | `session_models.RemoteAnalysisSettings` | Set False; readiness fails when `DEBUG=False` and mock on |
| Silent mock if API URL empty | `guacamole/client.py` | Empty `guacamole_api_url` still mocks — configure API URL with mock off |
| `virus_scanner=noop` | `workspace/scanner.py` | `defender`/`clamav` names still NoOp — accept risk or implement later |
| Open register without enrollment key | `views.register` | Set `RA_AGENT_ENROLLMENT_KEY` (readiness requires it when `DEBUG=False`) |
| Notification SMS/WhatsApp/Push stubs | `notifications/__init__.py` | Portal + email only |

*Source: [Cleanup search RA codebase](c30c584c-1c82-4287-8663-89009643e008).*

---

## Intentional (retain)

| Pattern | Why retained |
|---------|----------------|
| `mock_guacamole` code paths | Dev/test and CI without Guacamole |
| `NoOpScanner` | Explicit Milestone 5 design; extension point |
| `SessionRecording` metadata model | Placeholder for future recording — schema already shipped |
| Agent TFM `net10.0-windows` | Matches available SDK; not obsolete |
| Celery task `pass` / broad except in non-critical cleanup | Defensive; avoid cascading failures |
| `GuacamoleClientError` / `StorageError` empty `pass` classes | Exception markers |

---

## Search results (development artifacts)

### TODO / FIXME / HACK

No production `TODO`/`FIXME`/`HACK` markers found in `remote_analysis` Python package (tests excluded from “must clean”).

### mock

- Production feature flag `mock_guacamole` — **intentional**.
- Tests set mock True/False — **test-only**.
- Guacamole client `mock` property — **intentional**.

### debug / temporary / demo / sample

- No demo/sample endpoints in RA API.
- `validate_remote_analysis` prints sample task names — diagnostic only.

### placeholder / stub / NotImplemented

| Item | Path | Disposition |
|------|------|-------------|
| Session recording docstring | `session_models.py` | Document limitation |
| Virus scanner `NotImplementedError` on interface | `workspace/scanner.py` | Interface contract; NoOp used |
| SMS/WhatsApp/Push | `notifications/__init__.py` | Document limitation |

### unused / obsolete

| Check | Result |
|-------|--------|
| Unused migrations | None — 0001–0008 linear |
| Obsolete endpoints | None identified vs agent + frontend contracts |
| Duplicate schedulers | None |
| Stale catalog keys `PortalUrl` / `LocalApiPort` | Removed in WS4; RC1 release notes + scheduler/portal “future” sections corrected in Phase 3 follow-up |
| Dead services / DTOs | None identified in Phase 3 review |

### development configuration

| Item | Disposition |
|------|-------------|
| `appsettings` PortalBaseUrl default equip.iitr.ac.in | OK for IITR |
| Local Django settings | Not for production compose |
| Test `conftest` forces `mock_guacamole=True` | Correct |

---

## Recommended cleanup (non-blocking)

1. Ops checklist: assert `mock_guacamole=False` in production readiness probe (already fails readiness when mock off and Guacamole down).
2. Optionally filter ops UI to hide “recording available” if only metadata exists.
3. Do **not** delete mock Guacamole paths — required for automated tests (≥90% coverage).

---

## Conclusion

No mass deletion required. Remaining “mock/placeholder” items are **documented product limitations** or **test infrastructure**, not abandoned half-features blocking pilot once Guacamole is live and mock is disabled.
