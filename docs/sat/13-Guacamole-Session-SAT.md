# Guacamole Session SAT (SAT-11)

**Scope:** Browser remote desktop via Apache Guacamole  
**Primary path:** Mock Guacamole (`mock_guacamole=True`)  
**Lab path:** Live Guacamole when `SAT_GUAC=1` (and usually `SAT_LAB=1`)

Does **not** replace SAT-05 sync workflow. Desktop phases remain optional for sync-only commissioning.

## Cases

| ID | Case | Auto | Lab |
|----|------|------|-----|
| SAT-11.01 | Successful login / connect (mock) | ✓ | ✓ |
| SAT-11.02 | Unauthorized access | ✓ | ✓ |
| SAT-11.03 | Single active session per booking | ✓ | ✓ |
| SAT-11.04 | Idle timeout | ✓ | ✓ |
| SAT-11.05 | Forced disconnect | ✓ | ✓ |
| SAT-11.06 | Browser refresh (new launch token) | ✓ | ✓ |
| SAT-11.07 | Portal restart resilience | ✓ | ✓ |
| SAT-11.08 | Guacamole restart / re-provision | ✓ | ✓ live |
| SAT-11.09 | Network interruption → terminate + cleanup | ✓ | ✓ |
| SAT-11.10 | Analysis window not started → launch rejected | ✓ | ✓ |
| SAT-11.L1 | Live Guacamole health probe | — | `SAT_GUAC=1` |

## Run

```bash
# Mock (default)
pytest iic_booking/remote_analysis/tests/sat/test_sat_11_guacamole.py -m sat -q

# Live Guacamole
SAT_GUAC=1 pytest iic_booking/remote_analysis/tests/sat/test_sat_11_guacamole.py -m "sat or sat_lab" -q
```

## Pass criteria

- Owner can create → launch → connect in mock mode
- Non-owner / anonymous cannot launch
- Second create reuses open session when `single_active_session_per_booking=True`
- Idle and terminate paths reach terminal session status
- Launch tokens are single-use; refresh issues a new token
- Launch rejected when analysis window is too far in the future

## Related

- Architecture: [../RemoteAnalysisGuacamoleArchitecture.md](../RemoteAnalysisGuacamoleArchitecture.md)
- Runbook: [../RemoteAnalysisGuacamoleRunbook.md](../RemoteAnalysisGuacamoleRunbook.md)
