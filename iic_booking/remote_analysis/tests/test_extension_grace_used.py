"""Regression: RemoteDesktopSession must persist extension_grace_used (NOT NULL)."""

from __future__ import annotations

import pytest
from django.utils import timezone
from datetime import timedelta

from iic_booking.remote_analysis.constants import ReservationStatus, SessionStatus, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_remotedesktopsession_extension_grace_used_defaults_false():
    user = UserFactory()
    ws = AnalysisWorkstation.objects.create(
        agent_id="grace-default-ws",
        hostname="GRACE-DEFAULT",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=90,
    )
    now = timezone.now()
    reservation = AnalysisReservation.objects.create(
        user=user,
        workstation=ws,
        status=ReservationStatus.READY,
        requested_start=now,
        requested_end=now + timedelta(minutes=30),
        reserved_start=now,
        reserved_end=now + timedelta(minutes=30),
        priority=100,
    )
    session = RemoteDesktopSession.objects.create(
        reservation=reservation,
        user=user,
        workstation=ws,
        status=SessionStatus.CREATED,
    )
    session.refresh_from_db()
    assert session.extension_grace_used is False
