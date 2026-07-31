"""Performance / load scenarios (gated by ANALYSIS_PERF=1)."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from tests.analysis_platform.data_generator import (
    attach_workstations_to_pool,
    fake_raw_blob,
    make_bookings_for_equipment,
    make_mock_workstations,
)
from tests.analysis_platform.mock_agent import MockAnalysisAgent
from tests.analysis_platform.utils import finish_report, new_report


REPORT_DIR = Path(__file__).resolve().parent / "report"


@pytest.mark.analysis_perf
@pytest.mark.django_db
def test_perf_seed_and_summary_latency(analysis_perf_enabled, apt_seed, apt_researcher_api, apt_booking_id):
    report = new_report("analysis_perf_summary")
    latencies = []
    for _ in range(20):
        t0 = time.perf_counter()
        res = apt_researcher_api.get(f"/api/v1/bookings/{apt_booking_id}/analysis/")
        latencies.append((time.perf_counter() - t0) * 1000)
        assert res.status_code == 200
    report.metrics = {
        "summary_p50_ms": sorted(latencies)[len(latencies) // 2],
        "summary_max_ms": max(latencies),
        "summary_avg_ms": sum(latencies) / len(latencies),
    }
    finish_report(report)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report.write_json(REPORT_DIR / "perf_summary.json")
    report.write_html(REPORT_DIR / "perf_summary.html")
    assert report.metrics["summary_avg_ms"] < 2000


@pytest.mark.analysis_perf
@pytest.mark.django_db
def test_perf_50_bookings_list(analysis_perf_enabled, apt_seed, apt_researcher_api):
    bookings = make_bookings_for_equipment(apt_seed.equipment, count=50, user=apt_seed.researcher)
    t0 = time.perf_counter()
    ok = 0
    for b in bookings:
        res = apt_researcher_api.get(f"/api/v1/bookings/{b.booking_id}/analysis/")
        if res.status_code == 200:
            ok += 1
    elapsed = time.perf_counter() - t0
    assert ok == 50
    assert elapsed < 60.0, f"50 booking summaries took {elapsed:.2f}s"


@pytest.mark.analysis_perf
@pytest.mark.django_db(transaction=True)
def test_perf_heartbeat_burst(analysis_perf_enabled):
    workstations = make_mock_workstations(count=20)
    agents = []
    for ws, token in workstations:
        agent = MockAnalysisAgent(agent_id=ws.agent_id, hostname=ws.hostname)
        agent.state.token = token
        agent.state.registered = True
        agent.state.workstation_id = str(ws.id)
        agent._auth()
        agents.append(agent)

    t0 = time.perf_counter()
    for agent in agents:
        assert agent.heartbeat().get("accepted") is True
    elapsed = time.perf_counter() - t0
    assert elapsed < 30.0


@pytest.mark.analysis_perf
@pytest.mark.django_db(transaction=True)
def test_perf_concurrent_allocations(analysis_perf_enabled, apt_seed):
    """10 concurrent analyze requests across distinct bookings (soft latency gate)."""
    extra = make_mock_workstations(count=10)
    attach_workstations_to_pool(apt_seed.equipment, [ws for ws, _ in extra])
    for ws, token in extra:
        a = MockAnalysisAgent.from_seed(
            type("S", (), {"workstation": ws, "agent_token": token, "software": apt_seed.software})()
        )
        a.heartbeat()
        a.publish_inventory()

    bookings = make_bookings_for_equipment(apt_seed.equipment, count=10, user=apt_seed.researcher)
    workflow_id = str(apt_seed.single_step_workflow.id)

    def _analyze(booking_id: int):
        client = APIClient()
        client.force_authenticate(user=apt_seed.researcher)
        t0 = time.perf_counter()
        res = client.post(
            f"/api/v1/bookings/{booking_id}/analysis/analyze/",
            {"workflow_id": workflow_id},
            format="json",
        )
        return res.status_code, (time.perf_counter() - t0) * 1000

    times = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_analyze, b.booking_id) for b in bookings]
        for fut in as_completed(futures):
            code, ms = fut.result()
            assert code in {201, 202, 400}  # 400 if capacity exhausted mid-burst
            times.append(ms)

    assert times
    assert max(times) < 15000


@pytest.mark.analysis_perf
@pytest.mark.django_db
def test_perf_large_raw_blob_generation(analysis_perf_enabled):
    size_mb = int(os.environ.get("ANALYSIS_PERF_RAW_MB", "5"))
    t0 = time.perf_counter()
    blob = fake_raw_blob(size_mb=size_mb)
    elapsed = time.perf_counter() - t0
    assert len(blob) == size_mb * 1024 * 1024
    assert elapsed < 10.0
