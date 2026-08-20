"""Regression: AWAITING_CHECKIN must not appear as waiting queue."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, Equipment
from iic_booking.equipment.remote_analysis_integration.experience import AnalysisExperienceBuilder
from iic_booking.remote_analysis.constants import (
    QueueEntryStatus,
    ReservationStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationQueue
from iic_booking.remote_analysis.services.checkin import CheckinService
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_awaiting_checkin_not_queued_and_shows_allocated_message():
    user = UserFactory()
    equipment = Equipment.objects.create(
        code=f"PXRD-A-{timezone.now().timestamp():.0f}",
        name="PXRD [A]",
        enable_remote_analysis=True,
    )
    booking = Booking.objects.create(
        user=user,
        equipment=equipment,
        status=BookingStatus.COMPLETED,
        analysis_available=True,
        virtual_booking_id="IICPXRD [A]202600041",
        analysis_reservation=None,
    )
    ws = AnalysisWorkstation.objects.create(
        agent_id="r10-checkin-ws",
        hostname="RAVI",
        status=WorkstationStatus.RESERVED,
        enabled=True,
        health_score=100,
        last_heartbeat=timezone.now(),
    )
    now = timezone.now()
    reservation = AnalysisReservation.objects.create(
        user=user,
        booking=booking,
        workstation=ws,
        status=ReservationStatus.AWAITING_CHECKIN,
        requested_start=now,
        requested_end=now + timedelta(hours=4),
        reserved_start=now,
        reserved_end=now + timedelta(hours=4),
        priority=100,
    )
    CheckinService().open_checkin_window(reservation)
    reservation.refresh_from_db()

    ReservationQueue.objects.create(
        reservation=reservation,
        status=QueueEntryStatus.RESERVED,
        priority=100,
        enqueued_at=now,
        dequeued_at=now,
    )
    booking.analysis_reservation = reservation
    booking.save(update_fields=["analysis_reservation", "updated_at"])

    exp = AnalysisExperienceBuilder().build(booking)

    assert exp["awaiting_checkin"] is True
    assert exp["queue"]["is_queued"] is False
    assert exp["queue"]["title"] == "Analysis Environment Allocated"
    assert exp["checkin"]["required"] is True
    assert exp["checkin"]["remaining_seconds"] >= 0

    prepare = {step["id"]: step["status"] for step in exp["desktop_prepare"]}
    assert prepare["allocated"] == "done"
    assert prepare["sync_input"] == "pending"
    assert prepare["launch_desktop"] == "pending"
    assert prepare["ready"] == "pending"


@pytest.mark.django_db
def test_launch_without_rdp_secret_does_not_consume_checkin():
    """Missing WorkstationRdpSecret must fail before AWAITING_CHECKIN → RESERVED."""
    from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService
    from iic_booking.remote_analysis.guacamole.session import SessionError
    from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

    user = UserFactory()
    equipment = Equipment.objects.create(
        code=f"PXRD-A-{timezone.now().timestamp():.0f}",
        name="PXRD [A]",
        enable_remote_analysis=True,
    )
    booking = Booking.objects.create(
        user=user,
        equipment=equipment,
        status=BookingStatus.COMPLETED,
        analysis_available=True,
        virtual_booking_id="IICPXRD [A]202600047",
        analysis_reservation=None,
    )
    ws = AnalysisWorkstation.objects.create(
        agent_id="r10-checkin-ws-rdp",
        hostname="DESKTOP-CSMH6BU",
        status=WorkstationStatus.RESERVED,
        enabled=True,
        health_score=100,
        last_heartbeat=timezone.now(),
    )
    now = timezone.now()
    reservation = AnalysisReservation.objects.create(
        user=user,
        booking=booking,
        workstation=ws,
        status=ReservationStatus.AWAITING_CHECKIN,
        requested_start=now,
        requested_end=now + timedelta(hours=4),
        reserved_start=now,
        reserved_end=now + timedelta(hours=4),
        checkin_expires_at=now + timedelta(minutes=10),
        priority=100,
    )
    booking.analysis_reservation = reservation
    booking.save(update_fields=["analysis_reservation", "updated_at"])

    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.mock_guacamole = False
    settings_obj.save(update_fields=["mock_guacamole"])

    with pytest.raises(SessionError) as exc:
        BookingRemoteAnalysisService().launch_session(booking, user=user)
    assert exc.value.code == "rdp_credentials_missing"
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN

