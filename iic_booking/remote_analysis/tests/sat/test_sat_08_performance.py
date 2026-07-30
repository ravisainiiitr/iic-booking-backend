"""SAT-08 Performance (gated by SAT_PERF=1)."""

from __future__ import annotations

import time

import pytest
from rest_framework.test import APIClient


@pytest.mark.sat_perf
@pytest.mark.django_db
def test_sat_08_05_heartbeat_burst(sat_perf_enabled):
    """Synthetic burst: register N agents and heartbeat; record duration for baseline sheet."""
    api = APIClient()
    n = 20
    tokens = []
    for i in range(n):
        agent_id = f"sat-perf-hb-{i:02d}"
        res = api.post(
            "/api/v1/analysis/register/",
            {"agentId": agent_id, "hostname": agent_id, "cpuCores": 4, "memoryGB": 8},
            format="json",
        )
        assert res.status_code in (200, 201)
        tokens.append((agent_id, res.json()["token"]))

    t0 = time.perf_counter()
    for agent_id, token in tokens:
        hb = api.post(
            "/api/v1/analysis/heartbeat/",
            {"cpuPercent": 5, "memoryPercent": 20, "diskPercent": 30},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_X_AGENT_ID=agent_id,
        )
        assert hb.status_code in (200, 201)
    elapsed = time.perf_counter() - t0
    # Soft gate: 20 heartbeats should finish under 30s in test env
    assert elapsed < 30.0, f"Heartbeat burst too slow: {elapsed:.2f}s"
    print(f"SAT-08.05 20 heartbeats in {elapsed:.3f}s")  # noqa: T201


@pytest.mark.sat_perf
@pytest.mark.django_db
def test_sat_08_large_files_placeholder(sat_perf_enabled):
    pytest.skip("Measure 100MB/1GB on staging; record docs/sat/08-Performance-Baseline.md")
