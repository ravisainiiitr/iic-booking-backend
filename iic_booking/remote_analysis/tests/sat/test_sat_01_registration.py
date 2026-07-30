"""SAT-01 Agent Registration."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.remote_analysis.models import AnalysisWorkstation


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_01_01_first_registration():
    api = APIClient()
    res = api.post(
        "/api/v1/analysis/register/",
        {
            "agentId": "sat-reg-001",
            "hostname": "SAT-PC-01",
            "displayName": "SAT PC 01",
            "cpuCores": 8,
            "memoryGB": 32,
            "agentVersion": "sat-1.0",
        },
        format="json",
    )
    assert res.status_code in (200, 201)
    body = res.json()
    assert body.get("accepted") is True
    assert body.get("token")
    assert AnalysisWorkstation.objects.filter(agent_id="sat-reg-001").count() == 1


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_01_02_reregistration_same_agent_no_duplicate_row():
    api = APIClient()
    payload = {
        "agentId": "sat-reg-002",
        "hostname": "SAT-PC-02",
        "displayName": "SAT PC 02",
        "cpuCores": 4,
        "memoryGB": 16,
        "agentVersion": "sat-1.0",
    }
    first = api.post("/api/v1/analysis/register/", payload, format="json")
    assert first.status_code in (200, 201)
    assert first.json()["token"]

    payload["hostname"] = "SAT-PC-02-RENAMED"
    payload["agentVersion"] = "sat-1.1"
    second = api.post("/api/v1/analysis/register/", payload, format="json")
    assert second.status_code in (200, 201)
    body = second.json()
    assert body.get("accepted") is True
    assert AnalysisWorkstation.objects.filter(agent_id="sat-reg-002").count() == 1
    ws = AnalysisWorkstation.objects.get(agent_id="sat-reg-002")
    assert ws.hostname == "SAT-PC-02-RENAMED"
    # Product policy: re-register updates metadata; token may be omitted (agent keeps existing).
    assert body.get("created") is False
    assert "Already registered" in (body.get("message") or "") or body.get("token") is not None


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_01_05_invalid_enrollment_key(monkeypatch):
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "correct-enrollment-key")
    api = APIClient()
    res = api.post(
        "/api/v1/analysis/register/",
        {
            "agentId": "sat-reg-bad-key",
            "hostname": "SAT-BAD",
            "enrollmentKey": "wrong-key",
        },
        format="json",
    )
    assert res.status_code in (401, 403)
    body = res.json()
    assert body.get("accepted") is False
    assert AnalysisWorkstation.objects.filter(agent_id="sat-reg-bad-key").count() == 0


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_01_06_duplicate_agent_id_single_row():
    api = APIClient()
    for _ in range(3):
        api.post(
            "/api/v1/analysis/register/",
            {"agentId": "sat-reg-dup", "hostname": "DUP"},
            format="json",
        )
    assert AnalysisWorkstation.objects.filter(agent_id="sat-reg-dup").count() == 1


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_01_03_agent_restart_lab(sat_lab_enabled):
    """Manual/lab: restart Windows service; heartbeats resume with same agentId."""
    pytest.skip("Execute per docs/sat/01-Detailed-Checklist.md 01.03 with live agent.")


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_01_04_lost_token_recovery_lab(sat_lab_enabled):
    """Manual/lab: revoke/delete token; re-register with enrollment key."""
    pytest.skip("Execute per docs/sat/01-Detailed-Checklist.md 01.04 with live agent.")
