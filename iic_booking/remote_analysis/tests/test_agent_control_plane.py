"""Agent control-plane integration tests — register / heartbeat / inventory / commands."""

from __future__ import annotations

import json

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
def test_agent_register_creates_workstation_and_token(api):
    response = api.post(
        "/api/v1/analysis/register/",
        {
            "agentId": "raa-test-001",
            "hostname": "ANALYSIS-PC-01",
            "displayName": "Analysis PC 01",
            "cpuCores": 8,
            "memoryGB": 32,
            "agentVersion": "1.0.0",
        },
        format="json",
    )
    assert response.status_code in (200, 201)
    body = response.json()
    assert body["accepted"] is True
    assert body["agent_id"] == "raa-test-001"
    assert body["token"]
    assert AnalysisWorkstation.objects.filter(agent_id="raa-test-001").exists()


@pytest.mark.django_db
def test_agent_register_requires_agent_id(api):
    response = api.post("/api/v1/analysis/register/", {"hostname": "x"}, format="json")
    assert response.status_code == 400
    assert response.json()["accepted"] is False


@pytest.mark.django_db
def test_heartbeat_requires_auth(api):
    response = api.post("/api/v1/analysis/heartbeat/", {"CPU": 10}, format="json")
    assert response.status_code in (401, 403)


@pytest.mark.django_db
def test_heartbeat_updates_telemetry(api):
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-hb-1",
        hostname="HB-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    _, token = issue_agent_token(ws)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=ws.agent_id)

    response = api.post(
        "/api/v1/analysis/heartbeat/",
        {
            "CPU": 22.5,
            "Memory": 40.0,
            "Disk": 55.0,
            "LoggedInUser": "analyst",
            "CurrentStatus": "AVAILABLE",
            "Online": True,
            "Idle": False,
            "IdleTimeMinutes": 1,
            "WindowsUptimeHours": 12,
            "RunningProcesses": 100,
            "SoftwareCount": 5,
        },
        format="json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    ws.refresh_from_db()
    assert ws.last_heartbeat is not None
    assert ws.heartbeats.count() == 1


@pytest.mark.django_db
def test_inventory_sync(api):
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-inv-1",
        hostname="INV-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    _, token = issue_agent_token(ws)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=ws.agent_id)

    response = api.post(
        "/api/v1/analysis/inventory/",
        {
            "software": [
                {
                    "displayName": "OriginPro",
                    "version": "2024",
                    "publisher": "OriginLab",
                    "category": "analysis",
                }
            ],
            "hardware": {"cpuCores": 16, "memoryGB": 64},
        },
        format="json",
    )
    assert response.status_code == 200
    ws.refresh_from_db()
    assert ws.installed_software.filter(software_name="OriginPro", is_present=True).exists()


@pytest.mark.django_db
def test_command_poll_and_complete(api):
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-cmd-1",
        hostname="CMD-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    _, token = issue_agent_token(ws)
    cmd = CommandService().create_command(ws, CommandType.PING, payload={"ping": True})

    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=ws.agent_id)
    polled = api.get("/api/v1/analysis/commands/")
    assert polled.status_code == 200
    rows = polled.json()
    assert len(rows) == 1
    assert rows[0]["type"] == CommandType.PING
    assert rows[0]["id"] == str(cmd.id)
    assert rows[0]["payloadJson"]

    complete = api.post(
        f"/api/v1/analysis/commands/{cmd.id}/complete/",
        {"success": True, "message": "pong"},
        format="json",
    )
    assert complete.status_code == 200
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.COMPLETED
    assert cmd.result_message == "pong"


@pytest.mark.django_db
def test_prepare_command_payload_roundtrip(api):
    ws = AnalysisWorkstation.objects.create(
        agent_id="raa-prep-1",
        hostname="PREP-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )
    _, token = issue_agent_token(ws)
    cmd = CommandService().create_command(
        ws,
        CommandType.PREPARE_WORKSTATION,
        payload={"session_id": "sess-1", "local_path": "C:/data"},
    )
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=ws.agent_id)
    rows = api.get("/api/v1/analysis/commands/").json()
    payload = json.loads(rows[0]["payloadJson"])
    assert payload["session_id"] == "sess-1"
    assert payload["local_path"] == "C:/data"

    api.post(
        f"/api/v1/analysis/commands/{cmd.id}/complete/",
        {"success": True, "message": "Prepared"},
        format="json",
    )
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.COMPLETED
