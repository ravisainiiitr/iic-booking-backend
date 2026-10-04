"""Installer endpoint that stores the workstation Remote Desktop sign-in."""

from __future__ import annotations

import pytest
from rest_framework.test import APIRequestFactory

from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.guacamole.secrets import decrypt_password, encrypt_password
from iic_booking.remote_analysis.installer.views import save_rdp_secret
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.session_models import WorkstationRdpSecret

URL = "/api/v1/analysis/installer/rdp-secret/"


def _ws(agent_id: str) -> AnalysisWorkstation:
    return AnalysisWorkstation.objects.create(
        agent_id=agent_id,
        hostname=agent_id.upper(),
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
    )


def _post(body: dict, **headers):
    return save_rdp_secret(APIRequestFactory().post(URL, body, format="json", **headers))


@pytest.mark.django_db
def test_agent_token_stores_its_own_secret():
    ws = _ws("raa-rdp-own")
    _row, token = issue_agent_token(ws)
    resp = _post(
        {
            "workstationId": str(ws.id),
            "agentId": ws.agent_id,
            "rdpUsername": "raa-session",
            "rdpPassword": "N3w-Generated-Passw0rd!",
            "rdpDomain": "RAVI",
        },
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_ID=ws.agent_id,
    )
    assert resp.status_code == 200
    assert resp.data["rdp_secret_updated"] is True
    secret = WorkstationRdpSecret.objects.get(workstation=ws)
    assert secret.username == "raa-session"
    assert secret.domain == "RAVI"
    assert decrypt_password(secret.password_encrypted) == "N3w-Generated-Passw0rd!"


@pytest.mark.django_db
def test_update_keeps_security_mode():
    ws = _ws("raa-rdp-keep")
    WorkstationRdpSecret.objects.create(
        workstation=ws,
        username="ravi",
        password_encrypted=encrypt_password("old"),
        security="any",
    )
    _row, token = issue_agent_token(ws)
    resp = _post(
        {"workstationId": str(ws.id), "rdpUsername": "raa-session", "rdpPassword": "new-one"},
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert resp.status_code == 200
    secret = WorkstationRdpSecret.objects.get(workstation=ws)
    assert secret.username == "raa-session"
    assert secret.security == "any"
    assert decrypt_password(secret.password_encrypted) == "new-one"


@pytest.mark.django_db
def test_agent_token_cannot_change_another_workstation():
    mine = _ws("raa-rdp-mine")
    other = _ws("raa-rdp-other")
    _row, token = issue_agent_token(mine)
    resp = _post(
        {"workstationId": str(other.id), "rdpUsername": "x", "rdpPassword": "y"},
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_AGENT_ID=mine.agent_id,
    )
    assert resp.status_code == 403
    assert not WorkstationRdpSecret.objects.filter(workstation=other).exists()


@pytest.mark.django_db
def test_enrollment_key_alone_is_rejected(monkeypatch):
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "")
    ws = _ws("raa-rdp-anon")
    resp = _post(
        {"workstationId": str(ws.id), "rdpUsername": "x", "rdpPassword": "y"},
        HTTP_X_ENROLLMENT_KEY="",
    )
    assert resp.status_code == 403
    assert not WorkstationRdpSecret.objects.filter(workstation=ws).exists()


@pytest.mark.django_db
def test_password_is_required():
    ws = _ws("raa-rdp-nopass")
    _row, token = issue_agent_token(ws)
    resp = _post(
        {"workstationId": str(ws.id), "rdpUsername": "raa-session"},
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert resp.status_code == 400
