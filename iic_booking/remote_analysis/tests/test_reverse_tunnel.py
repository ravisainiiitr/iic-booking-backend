"""Tests for reverse tunnel tokens, lifecycle, and cutover validation."""

from __future__ import annotations

import time
from io import StringIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from iic_booking.remote_analysis.constants import (
    CommandStatus,
    CommandType,
    TransportMode,
    TunnelSessionStatus,
)
from iic_booking.remote_analysis.guacamole.settings_env import overlay_from_environ
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.tunnel import (
    TunnelGatewayClient,
    TunnelOrchestrator,
    TunnelTokenService,
)
from iic_booking.remote_analysis.tunnel_models import TunnelEvent, TunnelSession

# Deterministic non-production secret for TunnelTokenService under DEBUG=False.
TEST_RA_TUNNEL_TOKEN_SECRET = "test-ra-tunnel-secret-for-pytest"


@pytest.fixture
def ra_tunnel_token_secret(settings, monkeypatch):
    """Supply RA_TUNNEL_TOKEN_SECRET for tests that issue/verify tunnel tokens."""
    monkeypatch.setenv("RA_TUNNEL_TOKEN_SECRET", TEST_RA_TUNNEL_TOKEN_SECRET)
    settings.RA_TUNNEL_TOKEN_SECRET = TEST_RA_TUNNEL_TOKEN_SECRET
    return TEST_RA_TUNNEL_TOKEN_SECRET


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
def test_tunnel_token_issue_and_verify(eligible_workstation, ra_user, ra_tunnel_token_secret):
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
def test_tunnel_token_rejects_tamper(eligible_workstation, ra_user, ra_tunnel_token_secret):
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
def test_tunnel_token_expired(eligible_workstation, ra_user, monkeypatch, ra_tunnel_token_secret):
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


@pytest.mark.django_db(transaction=True)
def test_join_success_transitions_tunnel_active(eligible_workstation, ra_user):
    """JOIN_TUNNEL success activates via transaction.on_commit → apply_join_result."""
    tunnel = TunnelSession.objects.create(
        workstation=eligible_workstation,
        user=ra_user,
        nonce="nonce-join-ok",
        session_version=1,
        status=TunnelSessionStatus.WAITING_AGENT,
        adapter_hostname="reverse-tunnel-gateway",
        adapter_port=45000,
    )
    cmd = CommandService().create_command(
        eligible_workstation,
        CommandType.JOIN_TUNNEL,
        payload={"tunnel_id": str(tunnel.id), "wss_url": "wss://gw.example/tunnel"},
        created_by=ra_user,
    )
    CommandService().complete(cmd, success=True, message="joined")

    tunnel.refresh_from_db()
    assert tunnel.status == TunnelSessionStatus.ACTIVE
    assert tunnel.agent_joined_at is not None
    assert tunnel.activated_at is not None
    events = list(TunnelEvent.objects.filter(tunnel=tunnel).values_list("event_type", flat=True))
    assert "JOINED" in events
    assert "ACTIVE" in events
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_join_failure_transitions_tunnel_failed(eligible_workstation, ra_user):
    """JOIN_TUNNEL failure marks FAILED via transaction.on_commit → apply_join_result."""
    tunnel = TunnelSession.objects.create(
        workstation=eligible_workstation,
        user=ra_user,
        nonce="nonce-join-fail",
        session_version=1,
        status=TunnelSessionStatus.WAITING_AGENT,
    )
    cmd = CommandService().create_command(
        eligible_workstation,
        CommandType.JOIN_TUNNEL,
        payload={"tunnel_id": str(tunnel.id)},
        created_by=ra_user,
    )
    CommandService().complete(cmd, success=False, message="rdp unavailable")

    tunnel.refresh_from_db()
    assert tunnel.status == TunnelSessionStatus.FAILED
    assert "rdp unavailable" in tunnel.close_reason
    assert TunnelEvent.objects.filter(tunnel=tunnel, event_type="JOIN_FAILED").exists()
    cmd.refresh_from_db()
    assert cmd.status == CommandStatus.FAILED


@pytest.mark.django_db
def test_mock_allocator_disabled_in_production_reverse_tunnel(
    eligible_workstation, ra_user, settings
):
    settings.DEBUG = False
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
    settings_obj.tunnel_gateway_admin_url = ""
    settings_obj.save(update_fields=["transport_mode", "tunnel_gateway_admin_url"])

    tunnel = TunnelSession.objects.create(
        workstation=eligible_workstation,
        user=ra_user,
        nonce="nonce-mock-block",
        session_version=1,
    )
    client = TunnelGatewayClient(settings_obj)
    with pytest.raises(ImproperlyConfigured, match="RA_TUNNEL_GATEWAY_ADMIN_URL"):
        client.allocate(tunnel, token="unused")


@pytest.mark.django_db
def test_mock_allocator_allowed_when_debug_true(eligible_workstation, ra_user, settings):
    settings.DEBUG = True
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
    settings_obj.tunnel_gateway_admin_url = ""
    settings_obj.tunnel_adapter_hostname = "127.0.0.1"
    settings_obj.save(
        update_fields=["transport_mode", "tunnel_gateway_admin_url", "tunnel_adapter_hostname"]
    )
    tunnel = TunnelSession.objects.create(
        workstation=eligible_workstation,
        user=ra_user,
        nonce="nonce-mock-ok",
        session_version=1,
    )
    alloc = TunnelGatewayClient(settings_obj).allocate(tunnel, token="unused")
    assert alloc["mock"] is True
    assert alloc["gateway_instance"] == "local-mock"
    assert alloc["adapter_port"]


@pytest.mark.django_db
def test_readiness_direct_rdp_skips_guacamole_when_mock_and_debug_false(
    client, settings, monkeypatch
):
    """Production-shaped direct_rdp: mock Guacamole must not fail readiness."""
    settings.DEBUG = False
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "readiness-enroll-direct")
    monkeypatch.delenv("RA_TUNNEL_TOKEN_SECRET", raising=False)
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


@pytest.mark.django_db
def test_readiness_reverse_tunnel_requires_gateway_health(
    client, settings, monkeypatch
):
    settings.DEBUG = False
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "readiness-enroll-gw")
    monkeypatch.setenv("RA_TUNNEL_TOKEN_SECRET", TEST_RA_TUNNEL_TOKEN_SECRET)
    monkeypatch.setenv("RA_TUNNEL_GATEWAY_ADMIN_KEY", "unit-admin-key")
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
    settings_obj.mock_guacamole = False
    settings_obj.guacamole_api_url = "https://guac.example/api"
    settings_obj.guacamole_base_url = "https://guac.example/guacamole"
    settings_obj.guacamole_admin_username = "guacadmin"
    settings_obj.guacamole_admin_password = "secret"
    settings_obj.tunnel_gateway_admin_url = "http://reverse-tunnel-gateway:7090"
    settings_obj.tunnel_gateway_wss_url = "wss://gw.example/tunnel"
    settings_obj.tunnel_adapter_hostname = "reverse-tunnel-gateway"
    settings_obj.save()

    monkeypatch.setattr(
        "iic_booking.remote_analysis.guacamole.client.GuacamoleClient.health_check",
        lambda self: True,
    )
    monkeypatch.setattr(
        "iic_booking.remote_analysis.tunnel.TunnelGatewayClient.health",
        lambda self: {"ok": True, "status": "ok", "connected_agents": 0, "active_tunnels": 0},
    )

    response = client.get("/api/v1/analysis/health/ready/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["gateway"] == "ok"
    assert body["checks"]["tunnel_token_secret"] == "configured"
    assert body["checks"]["tunnel_gateway_admin_url"] == "configured"
    assert body["checks"]["tunnel_gateway_wss_url"] == "configured"
    assert body["checks"]["tunnel_adapter_hostname"] == "configured"
    assert body["checks"]["guacamole"] == "ok"


@pytest.mark.django_db
def test_readiness_reverse_tunnel_fails_when_gateway_down(
    client, settings, monkeypatch
):
    settings.DEBUG = False
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "readiness-enroll-gw-down")
    monkeypatch.setenv("RA_TUNNEL_TOKEN_SECRET", TEST_RA_TUNNEL_TOKEN_SECRET)
    monkeypatch.setenv("RA_TUNNEL_GATEWAY_ADMIN_KEY", "unit-admin-key")
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
    settings_obj.mock_guacamole = False
    settings_obj.guacamole_api_url = "https://guac.example/api"
    settings_obj.guacamole_base_url = "https://guac.example/guacamole"
    settings_obj.guacamole_admin_username = "guacadmin"
    settings_obj.guacamole_admin_password = "secret"
    settings_obj.tunnel_gateway_admin_url = "http://reverse-tunnel-gateway:7090"
    settings_obj.tunnel_gateway_wss_url = "wss://gw.example/tunnel"
    settings_obj.tunnel_adapter_hostname = "reverse-tunnel-gateway"
    settings_obj.save()

    monkeypatch.setattr(
        "iic_booking.remote_analysis.guacamole.client.GuacamoleClient.health_check",
        lambda self: True,
    )
    monkeypatch.setattr(
        "iic_booking.remote_analysis.tunnel.TunnelGatewayClient.health",
        lambda self: {"ok": False, "detail": "connection refused"},
    )

    response = client.get("/api/v1/analysis/health/ready/")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["gateway"].startswith("unreachable")


@pytest.mark.django_db
def test_readiness_reverse_tunnel_fails_missing_wss_and_secret(
    client, settings, monkeypatch
):
    settings.DEBUG = False
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "readiness-enroll-missing")
    monkeypatch.delenv("RA_TUNNEL_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("RA_TUNNEL_GATEWAY_ADMIN_KEY", raising=False)
    if hasattr(settings, "RA_TUNNEL_TOKEN_SECRET"):
        settings.RA_TUNNEL_TOKEN_SECRET = ""
    if hasattr(settings, "RA_TUNNEL_GATEWAY_ADMIN_KEY"):
        settings.RA_TUNNEL_GATEWAY_ADMIN_KEY = ""
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
    settings_obj.mock_guacamole = True
    settings_obj.tunnel_gateway_admin_url = "http://gw:7090"
    settings_obj.tunnel_gateway_wss_url = ""
    settings_obj.tunnel_adapter_hostname = "gw"
    settings_obj.save()

    monkeypatch.setattr(
        "iic_booking.remote_analysis.tunnel.TunnelGatewayClient.health",
        lambda self: {"ok": True, "status": "ok"},
    )

    response = client.get("/api/v1/analysis/health/ready/")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["tunnel_token_secret"] == "missing"
    assert body["checks"]["tunnel_gateway_admin_key"] == "missing"
    assert body["checks"]["tunnel_gateway_wss_url"] == "missing"


@pytest.mark.django_db
def test_validate_deployment_startup_fails_missing_tunnel_config_when_reverse_tunnel(
    settings, monkeypatch
):
    settings.DEBUG = False
    monkeypatch.setenv("DJANGO_SECRET_KEY", "unit-test-secret-key-not-for-prod")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "localhost")
    monkeypatch.setenv("RA_AGENT_ENROLLMENT_KEY", "enroll")
    monkeypatch.delenv("RA_TUNNEL_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("RA_TUNNEL_GATEWAY_ADMIN_KEY", raising=False)
    monkeypatch.delenv("RA_TUNNEL_GATEWAY_ADMIN_URL", raising=False)
    monkeypatch.delenv("RA_TUNNEL_GATEWAY_WSS_URL", raising=False)
    monkeypatch.delenv("RA_TUNNEL_ADAPTER_HOSTNAME", raising=False)
    if hasattr(settings, "RA_TUNNEL_TOKEN_SECRET"):
        settings.RA_TUNNEL_TOKEN_SECRET = ""
    if hasattr(settings, "RA_TUNNEL_GATEWAY_ADMIN_KEY"):
        settings.RA_TUNNEL_GATEWAY_ADMIN_KEY = ""

    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
    settings_obj.mock_guacamole = True
    settings_obj.tunnel_gateway_admin_url = ""
    settings_obj.tunnel_gateway_wss_url = ""
    settings_obj.tunnel_adapter_hostname = ""
    settings_obj.save()

    out = StringIO()
    err = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command(
            "validate_deployment_startup",
            "--skip-guacamole",
            stdout=out,
            stderr=err,
        )
    assert exc.value.code == 1
    combined = out.getvalue() + err.getvalue()
    assert "RA_TUNNEL_TOKEN_SECRET" in combined
    assert "RA_TUNNEL_GATEWAY_ADMIN_URL" in combined
    assert "RESULT: FAIL" in combined


@pytest.mark.django_db
def test_verify_reverse_tunnel_production_read_only(settings, monkeypatch):
    settings.DEBUG = False
    monkeypatch.setenv("RA_TUNNEL_TOKEN_SECRET", TEST_RA_TUNNEL_TOKEN_SECRET)
    monkeypatch.setenv("RA_TUNNEL_GATEWAY_ADMIN_KEY", "unit-admin-key")
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transport_mode = TransportMode.DIRECT_RDP
    settings_obj.tunnel_gateway_admin_url = "http://reverse-tunnel-gateway:7090"
    settings_obj.tunnel_gateway_wss_url = "wss://gw.example/tunnel"
    settings_obj.tunnel_adapter_hostname = "reverse-tunnel-gateway"
    settings_obj.save()

    monkeypatch.setattr(
        "iic_booking.remote_analysis.tunnel.TunnelGatewayClient.health",
        lambda self: {"ok": True, "status": "ok", "connected_agents": 0, "active_tunnels": 0},
    )
    monkeypatch.setattr(
        "iic_booking.remote_analysis.tunnel.TunnelGatewayClient.metrics",
        lambda self: {"ok": True, "connected_agents": 0, "active_tunnels": 0},
    )

    out = StringIO()
    call_command("verify_reverse_tunnel_production", stdout=out)
    text = out.getvalue()
    assert "RESULT: PASS" in text
    assert "migration.0015" in text
    assert "configured" in text
    # Never leak secrets
    assert TEST_RA_TUNNEL_TOKEN_SECRET not in text
    assert "unit-admin-key" not in text


@pytest.mark.django_db
def test_apply_join_result_does_not_revive_closed(eligible_workstation, ra_user):
    tunnel = TunnelSession.objects.create(
        workstation=eligible_workstation,
        user=ra_user,
        nonce="nonce-closed",
        session_version=1,
        status=TunnelSessionStatus.CLOSED,
    )
    TunnelOrchestrator().apply_join_result(tunnel, success=True, message="late")
    tunnel.refresh_from_db()
    assert tunnel.status == TunnelSessionStatus.CLOSED
