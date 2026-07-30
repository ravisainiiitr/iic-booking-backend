"""SAT-02 Heartbeat."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import HEARTBEAT_OFFLINE_SECONDS, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.health import calculate_health_score, update_workstation_health
from iic_booking.remote_analysis.services.tokens import issue_agent_token


def _register(api: APIClient, agent_id: str) -> tuple[AnalysisWorkstation, str]:
    res = api.post(
        "/api/v1/analysis/register/",
        {"agentId": agent_id, "hostname": agent_id.upper(), "cpuCores": 8, "memoryGB": 16},
        format="json",
    )
    assert res.status_code in (200, 201)
    token = res.json()["token"]
    ws = AnalysisWorkstation.objects.get(agent_id=agent_id)
    return ws, token


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_02_01_normal_heartbeat():
    api = APIClient()
    ws, token = _register(api, "sat-hb-001")
    res = api.post(
        "/api/v1/analysis/heartbeat/",
        {"cpuPercent": 10, "memoryPercent": 40, "diskPercent": 50},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_ID="sat-hb-001",
    )
    assert res.status_code in (200, 201)
    ws.refresh_from_db()
    assert ws.last_heartbeat is not None
    assert ws.health_score >= 0


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_02_02_03_offline_and_recovery():
    ws = AnalysisWorkstation.objects.create(
        agent_id="sat-hb-off",
        hostname="SAT-OFF",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=100,
        last_heartbeat=timezone.now() - timedelta(seconds=HEARTBEAT_OFFLINE_SECONDS + 30),
    )
    issue_agent_token(ws)
    stale_score = calculate_health_score(ws)
    update_workstation_health(ws)
    ws.refresh_from_db()
    assert ws.health_score == stale_score
    assert stale_score < 100

    ws.last_heartbeat = timezone.now()
    ws.save(update_fields=["last_heartbeat", "updated_at"])
    recovered = update_workstation_health(ws)
    assert recovered > stale_score


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_02_04_health_score_responds_to_age():
    fresh = AnalysisWorkstation(
        agent_id="sat-hb-fresh",
        hostname="F",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        last_heartbeat=timezone.now(),
        health_score=100,
    )
    stale = AnalysisWorkstation(
        agent_id="sat-hb-stale",
        hostname="S",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        last_heartbeat=timezone.now() - timedelta(seconds=HEARTBEAT_OFFLINE_SECONDS + 10),
        health_score=100,
    )
    assert calculate_health_score(fresh) > calculate_health_score(stale)


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_02_05_status_transitions_lab(sat_lab_enabled):
    pytest.skip("Lab: stop agent >90s, confirm OFFLINE; restart; confirm AVAILABLE/ONLINE.")
