"""Shared fixtures for Remote Analysis tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def ra_user(db):
    return UserFactory(user_type="admin", is_staff=True, is_superuser=True)


@pytest.fixture
def ra_settings(db):
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.mock_guacamole = True
    settings_obj.guacamole_api_url = ""
    settings_obj.guacamole_base_url = "https://guac.test/guacamole"
    settings_obj.save()
    return RemoteAnalysisSettings.get_solo()


@pytest.fixture
def eligible_workstation(db):
    ws = AnalysisWorkstation.objects.create(
        agent_id="ra-ws-eligible-1",
        hostname="ELIGIBLE-PC",
        display_name="Eligible PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=95,
        last_heartbeat=timezone.now(),
        supports_rdp=True,
        memory_gb=32,
        cpu_cores=8,
        storage_gb=500,
    )
    issue_agent_token(ws)
    return ws


@pytest.fixture
def reservation_window():
    start = timezone.now() + timedelta(minutes=5)
    end = start + timedelta(hours=2)
    return start, end
