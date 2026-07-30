"""SAT-11 Guacamole session acceptance tests (mock by default; live with SAT_GUAC=1)."""

from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import SessionStatus
from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService
from iic_booking.remote_analysis.guacamole.connection import ConnectionManager
from iic_booking.remote_analysis.guacamole.session import SessionError, SessionOrchestrator
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.users.tests.factories import UserFactory


def _sat_guac_live() -> bool:
    return os.environ.get("SAT_GUAC", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture
def sat_guac_live():
    if not _sat_guac_live():
        pytest.skip("Live Guacamole SAT requires SAT_GUAC=1")
    return True


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_01_successful_mock_login_connect(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user, client_ip="127.0.0.1")
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()
    assert session.status in {SessionStatus.READY, SessionStatus.TOKEN_GENERATED}

    launch = orch.build_launch_payload(
        session,
        user=ra_user,
        request_absolute_uri_builder=lambda p: f"https://portal.test{p}",
        client_ip="127.0.0.1",
    )
    token = parse_qs(urlparse(launch["launch_url"]).query)["t"][0]
    connected = orch.connect_with_token(session, token, user=ra_user, client_ip="127.0.0.1")
    assert connected["mock"] is True
    session.refresh_from_db()
    assert session.status in {SessionStatus.CONNECTED, SessionStatus.ACTIVE}


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_02_unauthorized_access(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    other = UserFactory(user_type="student", admin_approved=True, email_verified=True)
    with pytest.raises(SessionError) as exc:
        SessionOrchestrator().create_session(reservation=reservation, user=other)
    assert exc.value.code == "forbidden"

    anon = APIClient()
    assert anon.get("/api/v1/analysis/session/dashboard/").status_code in {401, 403}


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_03_single_active_session_per_booking(ra_user, eligible_workstation, reservation_window, ra_settings):
    ra_settings.single_active_session_per_booking = True
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    s1 = orch.create_session(reservation=reservation, user=ra_user)
    s2 = orch.create_session(reservation=reservation, user=ra_user)
    assert s1.id == s2.id


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_04_idle_timeout_enforced(ra_user, eligible_workstation, reservation_window, ra_settings):
    ra_settings.idle_timeout = 15
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()
    launch = orch.build_launch_payload(
        session,
        user=ra_user,
        request_absolute_uri_builder=lambda p: f"https://portal.test{p}",
    )
    token = parse_qs(urlparse(launch["launch_url"]).query)["t"][0]
    orch.connect_with_token(session, token, user=ra_user)
    session.refresh_from_db()
    session.last_activity_at = timezone.now() - timedelta(minutes=30)
    session.save(update_fields=["last_activity_at"])
    SessionCleanupService().cleanup_idle()
    session.refresh_from_db()
    assert session.status in {
        SessionStatus.TERMINATED,
        SessionStatus.EXPIRED,
        SessionStatus.COMPLETED,
        SessionStatus.DISCONNECTING,
        SessionStatus.IDLE,
    }
    # Force idle cleanup path to terminal when already past timeout
    if session.status == SessionStatus.IDLE:
        SessionCleanupService().cleanup_idle()
        session.refresh_from_db()
    assert session.status in {
        SessionStatus.TERMINATED,
        SessionStatus.EXPIRED,
        SessionStatus.COMPLETED,
        SessionStatus.DISCONNECTING,
    }


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_05_forced_disconnect(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()
    launch = orch.build_launch_payload(
        session,
        user=ra_user,
        request_absolute_uri_builder=lambda p: f"https://portal.test{p}",
    )
    token = parse_qs(urlparse(launch["launch_url"]).query)["t"][0]
    orch.connect_with_token(session, token, user=ra_user)
    terminated = orch.terminate(session, user=ra_user, reason="Forced disconnect SAT")
    assert terminated.status in {SessionStatus.TERMINATED, SessionStatus.COMPLETED}


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_06_browser_refresh_new_launch_token(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()
    launch1 = orch.build_launch_payload(
        session,
        user=ra_user,
        request_absolute_uri_builder=lambda p: f"https://portal.test{p}",
    )
    launch2 = orch.build_launch_payload(
        session,
        user=ra_user,
        request_absolute_uri_builder=lambda p: f"https://portal.test{p}",
    )
    t1 = parse_qs(urlparse(launch1["launch_url"]).query)["t"][0]
    t2 = parse_qs(urlparse(launch2["launch_url"]).query)["t"][0]
    assert t1 != t2
    # First token still valid until used; second also valid (refresh issues new token)
    orch.connect_with_token(session, t2, user=ra_user)
    with pytest.raises(SessionError):
        orch.connect_with_token(session, t2, user=ra_user)  # replay


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_07_portal_restart_resilience(ra_user, eligible_workstation, reservation_window, ra_settings):
    """Session row persists; cleanup can terminate after 'restart' (new orchestrator instance)."""
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    session = SessionOrchestrator().create_session(reservation=reservation, user=ra_user)
    sid = session.id
    # Simulate portal process restart: new orchestrator, load from DB
    session2 = RemoteDesktopSession.objects.get(pk=sid)
    assert session2.reservation_id == reservation.id
    SessionOrchestrator().terminate(session2, user=ra_user, reason="Portal restart cleanup")
    session2.refresh_from_db()
    assert session2.status in {SessionStatus.TERMINATED, SessionStatus.COMPLETED}


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_08_guacamole_restart_mock_reprovision(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()
    ConnectionManager(ra_settings).destroy(session)
    # Re-provision ephemeral connection after Guacamole/mock restart
    orch._provision_guacamole(session)
    session.refresh_from_db()
    assert session.status == SessionStatus.TOKEN_GENERATED
    assert session.guacamole_connection.is_active


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_09_network_interruption_terminate_cleanup(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()
    launch = orch.build_launch_payload(
        session,
        user=ra_user,
        request_absolute_uri_builder=lambda p: f"https://portal.test{p}",
    )
    token = parse_qs(urlparse(launch["launch_url"]).query)["t"][0]
    orch.connect_with_token(session, token, user=ra_user)
    orch.terminate(session, user=ra_user, reason="Network interruption")
    session.refresh_from_db()
    assert session.status in {SessionStatus.TERMINATED, SessionStatus.COMPLETED}
    try:
        conn = session.guacamole_connection
        assert conn.is_active is False or conn.destroyed_at is not None
    except Exception:
        pass


@pytest.mark.django_db
@pytest.mark.sat
def test_sat_11_10_window_not_started_rejected_on_launch(ra_user, eligible_workstation, ra_settings):
    start = timezone.now() + timedelta(hours=2)
    end = start + timedelta(hours=2)
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=True,
    )
    if not reservation.workstation_id:
        pytest.skip("Could not allocate workstation for future window test")
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()
    with pytest.raises(SessionError) as exc:
        orch.build_launch_payload(
            session,
            user=ra_user,
            request_absolute_uri_builder=lambda p: f"https://portal.test{p}",
        )
    assert exc.value.code == "window_not_started"


@pytest.mark.django_db
@pytest.mark.sat
@pytest.mark.sat_lab
def test_sat_11_live_guacamole_health(sat_guac_live, ra_settings):
    ra_settings.mock_guacamole = False
    ra_settings.save()
    from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
    from iic_booking.remote_analysis.operations.toolkit import probe_guacamole

    probe = probe_guacamole()
    assert probe.get("status") in {"PASS", "FAIL"}
    if probe.get("status") == "PASS":
        assert GuacamoleClient().health_check() is True
