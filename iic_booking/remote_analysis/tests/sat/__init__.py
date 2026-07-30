"""
System Acceptance Test harness (portal-automated + lab/perf gates).

Docs: docs/sat/README.md

  pytest iic_booking/remote_analysis/tests/sat -m sat
  SAT_LAB=1  pytest ... -m "sat or sat_lab"
  SAT_PERF=1 pytest ... -m sat_perf
"""
