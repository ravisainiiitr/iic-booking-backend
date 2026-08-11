"""Shared fixtures for Remote Analysis tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    ReservationStatus,
    TransportMode,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.tests.tunnel_test_config import (
    TEST_RA_TUNNEL_TOKEN_SECRET,
    apply_ra_tunnel_token_secret,
)
from iic_booking.users.tests.factories import UserFactory

# Re-export for tests that assert on the deterministic secret value.
__all__ = [
    "TEST_RA_TUNNEL_TOKEN_SECRET",
    "apply_ra_tunnel_token_secret",
    "complete_user_checkin",
    "ra_tunnel_token_secret",
    "ra_reverse_tunnel_test_config",
]


def complete_user_checkin(reservation, *, actor=None):
    """
    Advance allocation hold to post-check-in RESERVED.

    Auto-allocate ends in AWAITING_CHECKIN. Call this when a test needs
    post-check-in behaviour (extend, release-on-cancel, etc.).
    """
    reservation.refresh_from_db()
    if reservation.status != ReservationStatus.AWAITING_CHECKIN:
        return reservation
    ReservationService().transition(
        reservation,
        ReservationStatus.RESERVED,
        reason="User checked in",
        actor=actor,
    )
    reservation.checkin_expires_at = None
    reservation.save(update_fields=["checkin_expires_at", "updated_at"])
    reservation.refresh_from_db()
    return reservation


@pytest.fixture
def ra_tunnel_token_secret(settings, monkeypatch):
    """Supply RA_TUNNEL_TOKEN_SECRET for TunnelTokenService under DEBUG=False."""
    return apply_ra_tunnel_token_secret(settings, monkeypatch)


@pytest.fixture
def ra_reverse_tunnel_test_config(ra_tunnel_token_secret, settings, monkeypatch, db):
    """
    Shared production-style Reverse Tunnel pytest configuration.

    - Injects the deterministic test tunnel token secret.
    - Ensures reverse_tunnel transport + adapter hostname.
    - Allows local mock gateway allocation when admin URL is empty so
      Guacamole / session SAT tests can provision under DEBUG=False.
      (Production still requires RA_TUNNEL_GATEWAY_ADMIN_URL when DEBUG=False.)
    """
    from iic_booking.remote_analysis.tunnel import TunnelGatewayClient

    settings_obj = RemoteAnalysisSettings.get_solo()
    update_fields: list[str] = []
    if settings_obj.transport_mode != TransportMode.REVERSE_TUNNEL:
        settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
        update_fields.append("transport_mode")
    if not (settings_obj.tunnel_adapter_hostname or "").strip():
        settings_obj.tunnel_adapter_hostname = "127.0.0.1"
        update_fields.append("tunnel_adapter_hostname")
    if update_fields:
        settings_obj.save(update_fields=[*update_fields, "updated_at"])

    original_allocate = TunnelGatewayClient.allocate

    def _allocate_for_pytest(self, tunnel, *, token: str):
        if not (self.settings.tunnel_gateway_admin_url or "").strip():
            port = 40000 + (abs(hash(str(tunnel.id))) % 10000)
            return {
                "adapter_hostname": self.settings.tunnel_adapter_hostname or "127.0.0.1",
                "adapter_port": port,
                "gateway_instance": "local-mock",
                "mock": True,
            }
        return original_allocate(self, tunnel, token=token)

    monkeypatch.setattr(TunnelGatewayClient, "allocate", _allocate_for_pytest)
    return ra_tunnel_token_secret


@pytest.fixture
def ra_user(db):
    # admin_approved drives is_active on save; Session/Token auth require an active user.
    return UserFactory(
        user_type="admin",
        is_staff=True,
        is_superuser=True,
        admin_approved=True,
        email_verified=True,
    )


@pytest.fixture
def ra_settings(db, ra_reverse_tunnel_test_config):
    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.mock_guacamole = True
    settings_obj.guacamole_api_url = ""
    settings_obj.guacamole_base_url = "https://guac.test/guacamole"
    settings_obj.transport_mode = TransportMode.REVERSE_TUNNEL
    if not (settings_obj.tunnel_adapter_hostname or "").strip():
        settings_obj.tunnel_adapter_hostname = "127.0.0.1"
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
        last_inventory_update=timezone.now(),
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
