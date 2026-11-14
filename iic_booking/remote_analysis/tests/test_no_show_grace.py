"""No-show grace: keep working until the next user actually joins (14 Nov 2026)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import ReservationStatus, SessionStatus, WorkstationStatus
from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationEvent
from iic_booking.remote_analysis.services import handover
from iic_booking.remote_analysis.services.checkin import CheckinService
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.remote_analysis.tests.test_queue_fairness_and_holds import (
    _enqueue,
    _open_session,
    _reservation,
    _ws,
)

pytestmark = pytest.mark.django_db


def _current_user_on(ws, *, ended_ago=timedelta(seconds=5)):
    current = _reservation(status=ReservationStatus.ACTIVE, created_ago=timedelta(hours=1), workstation=ws)
    AnalysisReservation.objects.filter(pk=current.pk).update(
        reserved_start=timezone.now() - timedelta(hours=1), reserved_end=timezone.now() - ended_ago
    )
    current.refresh_from_db()
    session = _open_session(current, ws, ends_in=-ended_ago)
    return current, session


def _waiting_user(ago=timedelta(minutes=20)):
    nxt = _reservation(created_ago=ago)
    _enqueue(nxt, ago=ago)
    return nxt


def test_time_up_with_someone_waiting_hands_over_and_keeps_the_session_for_the_grace():
    ws = _ws([], status=WorkstationStatus.BUSY)
    current, session = _current_user_on(ws)
    nxt = _waiting_user()

    SessionCleanupService().cleanup_expired()

    session.refresh_from_db()
    nxt.refresh_from_db()
    ws.refresh_from_db()
    assert session.status == SessionStatus.ACTIVE
    remaining = session.expires_at - timezone.now()
    assert timedelta(minutes=9) < remaining <= timedelta(minutes=10)
    assert nxt.status == ReservationStatus.AWAITING_CHECKIN
    assert nxt.workstation_id == ws.id
    assert ws.status == WorkstationStatus.BUSY
    assert ReservationEvent.objects.filter(reservation=current, event_type="HANDOVER_STARTED").exists()
    state = handover.handover_state(session)
    assert state == {"active": True, "next_joined": False, "ends_at": session.expires_at.isoformat()}


def test_time_up_with_nobody_waiting_ends_the_session_as_before():
    ws = _ws([], status=WorkstationStatus.BUSY)
    _current, session = _current_user_on(ws)

    SessionCleanupService().cleanup_expired()

    session.refresh_from_db()
    assert session.status == SessionStatus.EXPIRED


def test_next_user_joining_gives_the_previous_user_two_minutes_and_asks_the_new_one_to_wait():
    ws = _ws([], status=WorkstationStatus.BUSY)
    current, session = _current_user_on(ws)
    nxt = _waiting_user()
    SessionCleanupService().cleanup_expired()
    nxt.refresh_from_db()

    pending = handover.on_next_user_joining(nxt)

    session.refresh_from_db()
    assert pending["launch_pending"] is True
    assert pending["status"] == "WAITING_PREVIOUS_USER"
    assert "previous user is saving" in pending["detail"]
    assert session.expires_at <= timezone.now() + timedelta(minutes=2, seconds=5)
    assert ReservationEvent.objects.filter(reservation=current, event_type="HANDOVER_JOINED").exists()
    AnalysisReservation.objects.filter(pk=nxt.pk).update(status=ReservationStatus.RESERVED)
    assert handover.handover_state(session)["next_joined"] is True


def test_joining_a_free_pc_goes_straight_ahead():
    ws = _ws([], status=WorkstationStatus.RESERVED)
    nxt = _reservation(status=ReservationStatus.AWAITING_CHECKIN, workstation=ws)
    assert handover.on_next_user_joining(nxt) is None


def test_next_user_not_joining_keeps_the_pc_busy_and_ends_the_grace_state():
    ws = _ws([], status=WorkstationStatus.BUSY)
    _current, session = _current_user_on(ws)
    nxt = _waiting_user()
    SessionCleanupService().cleanup_expired()
    nxt.refresh_from_db()
    AnalysisReservation.objects.filter(pk=nxt.pk).update(checkin_expires_at=timezone.now() - timedelta(seconds=1))

    CheckinService().expire_due()

    ws.refresh_from_db()
    session.refresh_from_db()
    assert ws.status == WorkstationStatus.BUSY  # the previous user is still on it
    assert session.status == SessionStatus.ACTIVE
    assert handover.handover_state(session) is None


def test_no_second_grace_while_the_next_user_already_holds_the_pc():
    ws = _ws([], status=WorkstationStatus.BUSY)
    _current, session = _current_user_on(ws)
    _waiting_user()
    SessionCleanupService().cleanup_expired()
    RemoteDesktopSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    _waiting_user(ago=timedelta(minutes=5))

    SessionCleanupService().cleanup_expired()

    session.refresh_from_db()
    assert session.status == SessionStatus.EXPIRED


def test_grace_never_runs_past_the_bookings_analysis_access():
    ws = _ws([], status=WorkstationStatus.BUSY)
    current, session = _current_user_on(ws)
    booking = current.booking
    booking.analysis_expiry = timezone.now() + timedelta(minutes=3)
    booking.save(update_fields=["analysis_expiry"])
    _waiting_user()

    SessionCleanupService().cleanup_expired()

    session.refresh_from_db()
    assert session.expires_at <= booking.analysis_expiry
