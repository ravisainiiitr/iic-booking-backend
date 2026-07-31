"""Mock Analysis Agent integration tests."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import CommandType
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.services.commands import CommandService
from tests.analysis_platform.mock_agent import MockAnalysisAgent


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_mock_agent_heartbeat_and_inventory(apt_mock_agent, apt_seed):
    hb = apt_mock_agent.heartbeat()
    assert hb.get("accepted") is True
    inv = apt_mock_agent.publish_inventory()
    assert inv is not None
    apt_seed.workstation.refresh_from_db()
    assert apt_seed.workstation.last_heartbeat is not None
    assert apt_seed.workstation.installed_software.filter(is_present=True).count() >= 1


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_mock_agent_handles_prepare_collect_clean(apt_seed):
    agent = MockAnalysisAgent.from_seed(apt_seed)
    agent.bootstrap()
    svc = CommandService()
    cmds = [
        svc.create_command(apt_seed.workstation, CommandType.PREPARE_WORKSTATION, payload={"workspace": "W1"}),
        svc.create_command(apt_seed.workstation, CommandType.COLLECT_WORKSPACE, payload={"workspace": "W1"}),
        svc.create_command(apt_seed.workstation, CommandType.CLEAN_WORKSTATION, payload={"workspace": "W1"}),
        svc.create_command(apt_seed.workstation, CommandType.PING, payload={}),
    ]
    agent.process_once()
    agent.process_once()
    completed = 0
    for cmd in cmds:
        cmd.refresh_from_db()
        if cmd.status == "COMPLETED":
            completed += 1
    assert completed >= 1
    assert len(agent.state.commands_completed) >= 1


@pytest.mark.analysis_platform
@pytest.mark.django_db
def test_mock_agent_fresh_register():
    api = APIClient()
    agent = MockAnalysisAgent(agent_id="apt-fresh-register-01", hostname="APT-FRESH-01", api=api)
    data = agent.register()
    assert data.get("accepted") is True
    assert agent.state.token
    assert agent.heartbeat().get("accepted") is True
