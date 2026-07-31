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
