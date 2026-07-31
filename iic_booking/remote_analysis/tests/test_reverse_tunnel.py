"""Tests for reverse tunnel tokens and transport settings."""

from __future__ import annotations

import time

import pytest

from iic_booking.remote_analysis.constants import TransportMode
from iic_booking.remote_analysis.guacamole.settings_env import overlay_from_environ
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.tunnel import TunnelTokenService
from iic_booking.remote_analysis.tunnel_models import TunnelSession


@pytest.mark.django_db
def test_transport_mode_defaults_direct_rdp():
    settings_obj = RemoteAnalysisSettings.get_solo()
    assert settings_obj.transport_mode == TransportMode.DIRECT_RDP


@pytest.mark.django_db
def test_ra_transport_env_overlay(monkeypatch):
    settings_obj, _ = RemoteAnalysisSettings.objects.get_or_create(pk=1)
    monkeypatch.setenv("RA_TRANSPORT", "reverse_tunnel")
    monkeypatch.setenv("RA_TUNNEL_GATEWAY_WSS_URL", "wss://gw.example/tunnel")
    overlay_from_environ(settings_obj)
    assert settings_obj.transport_mode == TransportMode.REVERSE_TUNNEL
    assert settings_obj.tunnel_gateway_wss_url == "wss://gw.example/tunnel"


@pytest.mark.django_db
def test_tunnel_token_issue_and_verify(eligible_workstation, ra_user):
    tunnel = TunnelSession.objects.create(
        workstation=eligible_workstation,
        user=ra_user,
        nonce="nonce-abc-123",
        session_version=1,
    )
    svc = TunnelTokenService()
    token = svc.issue(tunnel, lifetime_seconds=60)
    claims = svc.verify(token)
    assert claims.tunnel_id == str(tunnel.id)
    assert claims.workstation_id == str(eligible_workstation.id)
    assert claims.user_id == ra_user.pk
    assert claims.nonce == "nonce-abc-123"
    assert "rdp_bridge" in claims.permissions


@pytest.mark.django_db
def test_tunnel_token_rejects_tamper(eligible_workstation, ra_user):
    tunnel = TunnelSession.objects.create(
        workstation=eligible_workstation,
        user=ra_user,
        nonce="nonce-xyz",
        session_version=1,
    )
    token = TunnelTokenService().issue(tunnel, lifetime_seconds=60)
    bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(ValueError, match="signature|Malformed"):
        TunnelTokenService().verify(bad)


@pytest.mark.django_db
def test_tunnel_token_expired(eligible_workstation, ra_user, monkeypatch):
    tunnel = TunnelSession.objects.create(
        workstation=eligible_workstation,
        user=ra_user,
        nonce="nonce-exp",
        session_version=1,
    )
    svc = TunnelTokenService()
    real_now = time.time()
    monkeypatch.setattr("iic_booking.remote_analysis.tunnel.time.time", lambda: real_now)
    token = svc.issue(tunnel, lifetime_seconds=30)
    monkeypatch.setattr("iic_booking.remote_analysis.tunnel.time.time", lambda: real_now + 120)
    with pytest.raises(ValueError, match="expired"):
        svc.verify(token)


@pytest.mark.django_db
def test_readiness_direct_rdp_skips_guacamole_when_mock_and_debug_false(
    client, settings, monkeypatch
):
    """Production-shaped direct_rdp: mock Guacamole must not fail readiness."""
    settings.DEBUG = False
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "readiness-enroll-direct")
    monkeypatch.delenv("RA_TUNNEL_TOKEN_SECRET", raising=False)
    # Ensure Django setting fallback cannot satisfy a accidental secret check.
    if hasattr(settings, "RA_TUNNEL_TOKEN_SECRET"):
        settings.RA_TUNNEL_TOKEN_SECRET = ""
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.DIRECT_RDP
    settings_obj.mock_guacamole = True
    settings_obj.tunnel_gateway_admin_url = ""
    settings_obj.tunnel_gateway_wss_url = ""
    settings_obj.save(
        update_fields=[
            "transport_mode",
            "mock_guacamole",
            "tunnel_gateway_admin_url",
            "tunnel_gateway_wss_url",
        ]
    )

    response = client.get("/api/v1/analysis/health/ready/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["transport_mode"] == TransportMode.DIRECT_RDP
    assert body["checks"]["database"] == "ok"
    assert "cache" in body["checks"]
    assert body["checks"]["agent_enrollment"] == "configured"
    # Transport-specific infrastructure must not be evaluated or fail direct_rdp.
    for key in (
        "guacamole",
        "gateway",
        "tunnel",
        "tunnel_token_secret",
        "reverse_tunnel",
    ):
        assert key not in body["checks"]


@pytest.mark.django_db
def test_readiness_reverse_tunnel_still_forbids_mock_when_debug_false(
    client, settings, monkeypatch
):
    """reverse_tunnel keeps strict Guacamole readiness (mock + DEBUG=False → 503)."""
    settings.DEBUG = False
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "readiness-enroll-tunnel")
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
    settings_obj.mock_guacamole = True
    settings_obj.save(update_fields=["transport_mode", "mock_guacamole"])

    response = client.get("/api/v1/analysis/health/ready/")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["transport_mode"] == TransportMode.REVERSE_TUNNEL
    assert body["checks"]["guacamole"] == "mock_forbidden_when_debug_false"
    assert body["checks"]["agent_enrollment"] == "configured"
