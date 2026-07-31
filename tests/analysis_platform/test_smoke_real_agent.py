"""Real Windows Analysis Agent smoke tests (gated by ANALYSIS_LAB=1).

Stops before interacting with OriginPro / MATLAB / desktop apps.
"""

from __future__ import annotations

import os

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation


@pytest.mark.analysis_lab
@pytest.mark.django_db
def test_smoke_real_agent_online(analysis_lab_enabled):
    agent_id = (os.environ.get("ANALYSIS_AGENT_ID") or "").strip()
    if not agent_id:
        pytest.skip("Set ANALYSIS_AGENT_ID to the live Windows agent id.")

    ws = AnalysisWorkstation.objects.filter(agent_id=agent_id).first()
    assert ws is not None, f"Workstation {agent_id} not registered"
    assert ws.enabled is True
    assert ws.status in {
        WorkstationStatus.AVAILABLE,
        WorkstationStatus.BUSY,
        WorkstationStatus.ONLINE,
        WorkstationStatus.PREPARING,
    }
    assert ws.last_heartbeat is not None
    age = timezone.now() - ws.last_heartbeat
    assert age.total_seconds() < 300, f"Heartbeat stale ({age})"


@pytest.mark.analysis_lab
@pytest.mark.django_db
def test_smoke_software_inventory(analysis_lab_enabled):
    agent_id = (os.environ.get("ANALYSIS_AGENT_ID") or "").strip()
    if not agent_id:
        pytest.skip("Set ANALYSIS_AGENT_ID")
    ws = AnalysisWorkstation.objects.get(agent_id=agent_id)
    count = ws.installed_software.filter(is_present=True).count()
    assert count >= 1, "Agent advertised no installed software"


@pytest.mark.analysis_lab
@pytest.mark.django_db
def test_smoke_allocation_workspace_launch_url(analysis_lab_enabled, apt_seed, apt_researcher_api, apt_booking_id):
    """
    Allocate + launch against the live agent pool mapping if present.
    Does NOT open Guacamole or drive Windows UI.
    """
    agent_id = (os.environ.get("ANALYSIS_AGENT_ID") or "").strip()
    if not agent_id:
        pytest.skip("Set ANALYSIS_AGENT_ID")

    live = AnalysisWorkstation.objects.filter(agent_id=agent_id).first()
    assert live is not None
    from iic_booking.remote_analysis.catalog_models import EquipmentAnalysisPool

    EquipmentAnalysisPool.objects.get_or_create(
        equipment=apt_seed.equipment,
        workstation=live,
        defaults={"priority_boost": 50},
    )

    res = apt_researcher_api.post(
        f"/api/v1/bookings/{apt_booking_id}/analysis/analyze/",
        {"workflow_id": str(apt_seed.single_step_workflow.id)},
        format="json",
    )
    assert res.status_code in {201, 202}
    body = res.json()
    assert body.get("eligible") is not False
    # Guacamole launch URL present when allocated (mock_guacamole may still be on in lab — either OK)
    assert body.get("launcher_url") or body.get("launch_url") or body.get("queued")


@pytest.mark.analysis_lab
@pytest.mark.django_db
def test_smoke_agent_heartbeat_endpoint_auth(analysis_lab_enabled):
    """Ensure anonymous cannot spoof live agent heartbeats."""
    api = APIClient()
    res = api.post("/api/v1/analysis/heartbeat/", {"CPU": 1}, format="json")
    assert res.status_code in {401, 403}
