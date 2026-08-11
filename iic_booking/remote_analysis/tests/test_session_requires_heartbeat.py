"""Session prepare requires a fresh agent heartbeat (not soft-online alone)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.guacamole.health import workstation_healthy_for_session
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.tokens import issue_agent_token


@pytest.mark.django_db
def test_session_health_requires_fresh_heartbeat_even_with_token():
    ws = AnalysisWorkstation.objects.create(
        agent_id="prep-needs-hb",
        hostname="PREP-NEEDS-HB",
        status=WorkstationStatus.RESERVED,
        enabled=True,
        health_score=90,
        last_heartbeat=None,
    )
    issue_agent_token(ws)
    assert AvailabilityEngine().agent_online(ws) is True  # soft-online for allocation
    assert AvailabilityEngine().heartbeat_fresh(ws) is False
    assert workstation_healthy_for_session(ws) is False


@pytest.mark.django_db
def test_session_health_ok_with_fresh_heartbeat():
    ws = AnalysisWorkstation.objects.create(
        agent_id="prep-has-hb",
        hostname="PREP-HAS-HB",
        status=WorkstationStatus.PREPARING,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now() - timedelta(seconds=10),
    )
    assert workstation_healthy_for_session(ws) is True
