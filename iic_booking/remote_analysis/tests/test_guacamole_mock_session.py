"""Guacamole mock session lifecycle tests (WS3)."""

from __future__ import annotations

import pytest

from iic_booking.remote_analysis.constants import ReservationStatus, SessionStatus
from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
from iic_booking.remote_analysis.guacamole.session import SessionError, SessionOrchestrator
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_mock_guacamole_client_authenticate(ra_settings):
    client = GuacamoleClient(ra_settings)
    assert client.mock is True
    token = client.authenticate()
    assert token.startswith("mock-token-")


@pytest.mark.django_db
def test_session_create_launch_connect_terminate(
    ra_user, eligible_workstation, reservation_window, ra_settings
):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN

    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user, client_ip="127.0.0.1")
    session.refresh_from_db()
    assert session.status in {
        SessionStatus.READY,
        SessionStatus.TOKEN_GENERATED,
        SessionStatus.PREPARING,
    }
    # Mock path should advance past preparing
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
    assert launch["session_id"] == str(session.id)
    assert launch["mock"] is True
    assert "launch_url" in launch
    assert "t=" in launch["launch_url"]

    # Extract token from launch URL
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(launch["launch_url"]).query)
    plaintext = qs["t"][0]

    connected = orch.connect_with_token(
        session, plaintext, user=ra_user, client_ip="127.0.0.1", user_agent="pytest"
    )
    assert connected["mock"] is True
    assert connected["mock_desktop"] is True
    session.refresh_from_db()
    assert session.status in {
        SessionStatus.CONNECTED,
        SessionStatus.ACTIVE,
        SessionStatus.CONNECTING,
    }

    terminated = orch.terminate(session, user=ra_user, reason="Test done")
    assert terminated.status in {
        SessionStatus.TERMINATED,
        SessionStatus.COMPLETED,
    }


@pytest.mark.django_db
def test_session_create_rejects_inactive_reservation(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=False,
    )
    with pytest.raises(SessionError) as exc:
        SessionOrchestrator().create_session(reservation=reservation, user=ra_user)
    assert exc.value.code in {"reservation_inactive", "no_workstation"}


@pytest.mark.django_db
def test_session_launch_forbidden_for_other_user(
    ra_user, eligible_workstation, reservation_window, ra_settings
):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()

    other = UserFactory(user_type="faculty")
    with pytest.raises(SessionError) as exc:
        orch.issue_launch_token(session, user=other)
    assert exc.value.code == "forbidden"


@pytest.mark.django_db
def test_token_replay_rejected(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()

    _, plaintext = orch.issue_launch_token(session, user=ra_user, client_ip="127.0.0.1")
    orch.connect_with_token(session, plaintext, user=ra_user, client_ip="127.0.0.1")
    with pytest.raises(SessionError) as exc:
        orch.connect_with_token(session, plaintext, user=ra_user, client_ip="127.0.0.1")
    assert exc.value.code == "token_replay"
