"""Control-plane edge cases (WS3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import CommandStatus, CommandType, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.services.tokens import issue_agent_token


@pytest.fixture
def api():
    return APIClient()


@pytest.mark.django_db
def test_reregister_updates_workstation_keeps_or_issues_token(api):
    first = api.post(
        "/api/v1/analysis/register/",
        {"agentId": "raa-rereg-1", "hostname": "REREG-PC", "cpuCores": 4, "memoryGB": 16},
        format="json",
    )
    assert first.status_code in (200, 201)
    assert first.json()["token"]
    old_token = first.json()["token"]

    second = api.post(
        "/api/v1/analysis/register/",
        {"agentId": "raa-rereg-1", "hostname": "REREG-PC", "cpuCores": 8, "memoryGB": 32},
        format="json",
    )
    assert second.status_code in (200, 201)
    body = second.json()
    assert body["accepted"] is True
    # Enrollment-only re-register rotates and returns new plaintext
    assert body.get("token")
    assert body["token"] != old_token
    ws = AnalysisWorkstation.objects.get(agent_id="raa-rereg-1")
    assert ws.cpu_cores == 8
    assert ws.tokens.filter(is_active=True).count() == 1


@pytest.mark.django_db
def test_enrollment_reregister_rotates_token_when_no_bearer(api, monkeypatch):
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "phase3-enroll-secret")
    first = api.post(
        "/api/v1/analysis/register/",
        {"agentId": "raa-rot-1", "hostname": "ROT-PC", "cpuCores": 4, "memoryGB": 16},
        format="json",
        HTTP_X_ENROLLMENT_KEY="phase3-enroll-secret",
    )
    assert first.status_code in (200, 201)
    old_token = first.json()["token"]
    assert old_token

    second = api.post(
        "/api/v1/analysis/register/",
        {"agentId": "raa-rot-1", "hostname": "ROT-PC", "cpuCores": 4, "memoryGB": 16},
        format="json",
        HTTP_X_ENROLLMENT_KEY="phase3-enroll-secret",
    )
    assert second.status_code == 200
    new_token = second.json()["token"]
    assert new_token
    assert new_token != old_token

    # Old token invalid for heartbeat
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {old_token}", HTTP_X_AGENT_ID="raa-rot-1")
    bad = api.post(
        "/api/v1/analysis/heartbeat/",
        {"CPU": 10, "Memory": 10, "Disk": 10, "CurrentStatus": "AVAILABLE", "Online": True},
        format="json",
    )
    assert bad.status_code == 401

    # New token works
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {new_token}", HTTP_X_AGENT_ID="raa-rot-1")
    good = api.post(
        "/api/v1/analysis/heartbeat/",
        {"CPU": 10, "Memory": 10, "Disk": 10, "CurrentStatus": "AVAILABLE", "Online": True},
        format="json",
    )
    assert good.status_code == 200
    assert good.json()["accepted"] is True


@pytest.mark.django_db
def test_reregister_with_valid_bearer_keeps_token(api, monkeypatch):
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "phase3-enroll-secret")
    first = api.post(
        "/api/v1/analysis/register/",
        {"agentId": "raa-keep-1", "hostname": "KEEP-PC", "cpuCores": 4, "memoryGB": 16},
        format="json",
        HTTP_X_ENROLLMENT_KEY="phase3-enroll-secret",
    )
    token = first.json()["token"]

    second = api.post(
        "/api/v1/analysis/register/",
        {"agentId": "raa-keep-1", "hostname": "KEEP-PC-2", "cpuCores": 8, "memoryGB": 16},
        format="json",
        HTTP_X_ENROLLMENT_KEY="phase3-enroll-secret",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_ID="raa-keep-1",
    )
    assert second.status_code == 200
    body = second.json()
    # Valid Bearer: plaintext may be omitted; old token still valid
    assert body.get("token") in (None, "", token)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID="raa-keep-1")
    hb = api.post(
        "/api/v1/analysis/heartbeat/",
        {"CPU": 1, "Memory": 1, "Disk": 1, "CurrentStatus": "AVAILABLE", "Online": True},
        format="json",
    )
    assert hb.status_code == 200
    ws = AnalysisWorkstation.objects.get(agent_id="raa-keep-1")
    assert ws.hostname == "KEEP-PC-2"


@pytest.mark.django_db
def test_heartbeat_high_cpu_still_accepted(api):
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-cpu-hot",
        hostname="HOT-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    _, token = issue_agent_token(ws)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=ws.agent_id)
    response = api.post(
        "/api/v1/analysis/heartbeat/",
        {
            "CPU": 99.5,
            "Memory": 80.0,
            "Disk": 70.0,
            "CurrentStatus": "AVAILABLE",
            "Online": True,
        },
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True


@pytest.mark.django_db
def test_command_expires_before_delivery(api):
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-exp-cmd",
        hostname="EXP-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    _, token = issue_agent_token(ws)
    cmd = CommandService().create_command(ws, CommandType.PING, payload={"x": 1})
    RemoteCommand.objects.filter(pk=cmd.pk).update(expires_at=timezone.now() - timedelta(minutes=1))

    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=ws.agent_id)
    rows = api.get("/api/v1/analysis/commands/").json()
    assert rows == []
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.EXPIRED


@pytest.mark.django_db
def test_command_failure_reported(api):
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-fail-cmd",
        hostname="FAIL-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    _, token = issue_agent_token(ws)
    cmd = CommandService().create_command(ws, CommandType.PING, payload={})
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=ws.agent_id)
    api.get("/api/v1/analysis/commands/")
    complete = api.post(
        f"/api/v1/analysis/commands/{cmd.id}/complete/",
        {"success": False, "message": "boom", "error": "agent error"},
        format="json",
    )
    assert complete.status_code == 200
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.FAILED


@pytest.mark.django_db
def test_wrong_agent_token_rejected(api):
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-auth-a",
        hostname="A",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    other = AnalysisWorkstation.objects.create(
        agent_id="raa-auth-b",
        hostname="B",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    _, token = issue_agent_token(ws)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=other.agent_id)
    response = api.post("/api/v1/analysis/heartbeat/", {"CPU": 1, "Online": True}, format="json")
    assert response.status_code in (401, 403)
