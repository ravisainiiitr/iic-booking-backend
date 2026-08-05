"""Allocation / availability unit tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.allocation import AllocationService
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.tokens import issue_agent_token


@pytest.mark.django_db
def test_offline_workstation_not_available():
    ws = AnalysisWorkstation.objects.create(
        agent_id="alloc-off-1",
        hostname="OFF",
        status=WorkstationStatus.OFFLINE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now(),
    )
    issue_agent_token(ws)
    start = timezone.now()
    end = start + timedelta(hours=1)
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is False
    assert result.reasons


@pytest.mark.django_db
def test_available_workstation_with_fresh_heartbeat():
    ws = AnalysisWorkstation.objects.create(
        agent_id="alloc-ok-1",
        hostname="OK",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=95,
        last_heartbeat=timezone.now(),
        supports_rdp=True,
    )
    issue_agent_token(ws)
    start = timezone.now()
    end = start + timedelta(hours=1)
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is True


@pytest.mark.django_db
def test_available_workstation_with_stale_heartbeat_and_valid_token():
    """Soft-online: stale heartbeat does not block AVAILABLE + enabled + valid token."""
    ws = AnalysisWorkstation.objects.create(
        agent_id="alloc-soft-1",
        hostname="SOFT",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=95,
        last_heartbeat=timezone.now() - timedelta(minutes=30),
        supports_rdp=True,
    )
    issue_agent_token(ws)
    start = timezone.now()
    end = start + timedelta(hours=1)
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is True, result.reasons


@pytest.mark.django_db
def test_score_workstation_returns_candidate():
    ws = AnalysisWorkstation.objects.create(
        agent_id="alloc-score-1",
        hostname="SCORE",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=88,
        last_heartbeat=timezone.now(),
    )
    issue_agent_token(ws)
    start = timezone.now()
    end = start + timedelta(hours=2)
    scored = AllocationService().score_workstation(ws, start=start, end=end)
    assert scored.workstation.id == ws.id
    assert isinstance(scored.score, float)
    assert "health_score" in scored.breakdown
