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

    second = api.post(
        "/api/v1/analysis/register/",
        {"agentId": "raa-rereg-1", "hostname": "REREG-PC", "cpuCores": 8, "memoryGB": 32},
        format="json",
    )
    assert second.status_code in (200, 201)
    body = second.json()
    assert body["accepted"] is True
    # Re-register keeps existing token (plaintext may be omitted)
    assert "token" in body
    ws = AnalysisWorkstation.objects.get(agent_id="raa-rereg-1")
    assert ws.cpu_cores == 8
    assert ws.tokens.filter(is_active=True).exists()


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
