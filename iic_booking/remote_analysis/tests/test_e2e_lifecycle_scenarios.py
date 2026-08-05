"""E2E-style lifecycle scenarios for Remote Analysis (WS3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import ReservationStatus, SessionStatus, WorkstationStatus
from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.scheduler import SchedulerService
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.tests.conftest import complete_user_checkin
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_scenario_happy_path_book_launch_end(ra_user, eligible_workstation, reservation_window, ra_settings):
    """Scenario 1: reserve → create session → launch → connect → terminate."""
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN

    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()

    launch = orch.build_launch_payload(
        session, user=ra_user, request_absolute_uri_builder=lambda p: p, client_ip="10.0.0.1"
    )
    from urllib.parse import parse_qs, urlparse

    token = parse_qs(urlparse(launch["launch_url"]).query)["t"][0]
    orch.connect_with_token(session, token, user=ra_user, client_ip="10.0.0.1")
    orch.terminate(session, user=ra_user, reason="Done")
    session.refresh_from_db()
    assert session.status in {SessionStatus.TERMINATED, SessionStatus.COMPLETED}


@pytest.mark.django_db
def test_scenario_queue_then_allocate(ra_user, reservation_window):
    """Scenario 2: no capacity → queue → workstation comes online → allocate."""
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    assert reservation.status == ReservationStatus.QUEUED

    ws = AnalysisWorkstation.objects.create(
        agent_id="e2e-queue-ws",
        hostname="E2E-Q",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=88,
        last_heartbeat=timezone.now(),
        supports_rdp=True,
    )
    issue_agent_token(ws)
    SchedulerService().process_queue()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN


@pytest.mark.django_db
def test_scenario_cancel_before_session(ra_user, eligible_workstation, reservation_window):
    """Scenario 3: cancel reserved slot before launching desktop."""
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    ReservationService().cancel(reservation, actor=ra_user)
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.CANCELLED


@pytest.mark.django_db
def test_scenario_extend_active_window(ra_user, eligible_workstation, reservation_window):
    """Scenario 4: extend reserved end successfully."""
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    complete_user_checkin(reservation, actor=ra_user)
    extended = ReservationService().extend(reservation, end + timedelta(minutes=45), actor=ra_user)
    assert extended.reserved_end > end


@pytest.mark.django_db
def test_scenario_expire_unused_reservation(ra_user, eligible_workstation):
    """Scenario 5: unused reservation past start grace is expired."""
    start = timezone.now() - timedelta(hours=2)
    end = timezone.now() + timedelta(hours=1)
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    reservation = AnalysisReservation.objects.create(
        user=ra_user,
        workstation=eligible_workstation,
        status=ReservationStatus.RESERVED,
        requested_start=start,
        requested_end=end,
        reserved_start=start,
        reserved_end=end,
        priority=100,
    )
    SchedulerService().expire_stale()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.EXPIRED


@pytest.mark.django_db
def test_scenario_second_user_queues_while_busy(ra_user, eligible_workstation, reservation_window):
    """Scenario 6: only one WS — second user is queued."""
    start, end = reservation_window
    first = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    assert first.status == ReservationStatus.AWAITING_CHECKIN

    other = UserFactory(user_type="admin", is_staff=True, is_superuser=True)
    second = ReservationService().create_reservation(
        user=other, requested_start=start, requested_end=end, created_by=other
    )
    assert second.status == ReservationStatus.QUEUED


@pytest.mark.django_db
def test_scenario_reject_session_after_cancel(ra_user, eligible_workstation, reservation_window, ra_settings):
    """Scenario 7: cancelled reservation cannot start a session."""
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    ReservationService().cancel(reservation, actor=ra_user)
    from iic_booking.remote_analysis.guacamole.session import SessionError

    with pytest.raises(SessionError):
        SessionOrchestrator().create_session(reservation=reservation, user=ra_user)
