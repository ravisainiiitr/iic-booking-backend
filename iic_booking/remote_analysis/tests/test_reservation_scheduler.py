"""Reservation + scheduler service tests (WS3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import ReservationStatus, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationQueue
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.scheduler import SchedulerService
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.tests.conftest import complete_user_checkin
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_create_reservation_allocates_when_candidate_exists(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=True,
    )
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN
    assert reservation.workstation_id == eligible_workstation.id
    assert reservation.allocation_score is not None
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.RESERVED


@pytest.mark.django_db
def test_create_reservation_queues_when_no_candidate(ra_user, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=True,
    )
    assert reservation.status == ReservationStatus.QUEUED
    assert ReservationQueue.objects.filter(reservation=reservation).exists()


@pytest.mark.django_db
def test_create_reservation_rejects_bad_window(ra_user):
    now = timezone.now()
    with pytest.raises(ValueError, match="requested_end"):
        ReservationService().create_reservation(
            user=ra_user,
            requested_start=now,
            requested_end=now,
            created_by=ra_user,
        )


@pytest.mark.django_db
def test_cancel_reserved_releases_reservation(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    complete_user_checkin(reservation, actor=ra_user)
    cancelled = ReservationService().cancel(reservation, actor=ra_user, reason="User cancel")
    assert cancelled.status == ReservationStatus.CANCELLED
    assert cancelled.released_at is not None


@pytest.mark.django_db
def test_extend_updates_reserved_end(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    complete_user_checkin(reservation, actor=ra_user)
    new_end = end + timedelta(hours=1)
    extended = ReservationService().extend(reservation, new_end, actor=ra_user)
    assert extended.reserved_end == new_end


@pytest.mark.django_db
def test_extend_blocked_by_overlap(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    first = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    complete_user_checkin(first, actor=ra_user)
    other = UserFactory(user_type="admin", is_staff=True)
    # Second reservation overlapping same workstation — queue or allocate elsewhere.
    # Force a reserved reservation on same WS for conflict.
    AnalysisReservation.objects.create(
        user=other,
        workstation=eligible_workstation,
        status=ReservationStatus.RESERVED,
        requested_start=end,
        requested_end=end + timedelta(hours=2),
        reserved_start=end,
        reserved_end=end + timedelta(hours=2),
        priority=100,
    )
    with pytest.raises(ValueError, match="Extension conflicts"):
        ReservationService().extend(first, end + timedelta(hours=3), actor=ra_user)


@pytest.mark.django_db
def test_process_queue_allocates_waiting(ra_user, reservation_window):
    start, end = reservation_window
    # Queue first (no workstation)
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=True,
    )
    assert reservation.status == ReservationStatus.QUEUED

    # Bring a workstation online
    ws = AnalysisWorkstation.objects.create(
        agent_id="ra-ws-queue-1",
        hostname="QUEUE-PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now(),
        supports_rdp=True,
    )
    issue_agent_token(ws)

    result = SchedulerService().process_queue(limit=5)
    reservation.refresh_from_db()
    assert result["allocated"] >= 1
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN
    assert reservation.workstation_id == ws.id


@pytest.mark.django_db
def test_expire_stale_past_reserved_end(ra_user, eligible_workstation):
    past_start = timezone.now() - timedelta(hours=3)
    past_end = timezone.now() - timedelta(hours=1)
    reservation = AnalysisReservation.objects.create(
        user=ra_user,
        workstation=eligible_workstation,
        status=ReservationStatus.RESERVED,
        requested_start=past_start,
        requested_end=past_end,
        reserved_start=past_start,
        reserved_end=past_end,
        priority=100,
    )
    stats = SchedulerService().expire_stale()
    reservation.refresh_from_db()
    assert stats["expired"] >= 1
    assert reservation.status in {ReservationStatus.COMPLETED, ReservationStatus.EXPIRED}
