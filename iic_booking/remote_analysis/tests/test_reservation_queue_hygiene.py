"""Regression: reservation queue hygiene (missed check-in order, ineligible holds, session extension).

Production 2026-10 (IICDSATEST202600016/17/19): a missed check-in kept its original queue slot and
re-won the only MultiPak PC every few minutes; refunded bookings held that PC for days; and
extending a session moved only the session expiry, so the end-of-window sweep could free the PC
under a live desktop.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.equipment.remote_analysis_integration import session_close
from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService
from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService
from iic_booking.remote_analysis.constants import (
    QueueEntryStatus,
    ReservationStatus,
    SessionStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.guacamole.session import SessionError
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationQueue
from iic_booking.remote_analysis.services.checkin import CheckinService
from iic_booking.remote_analysis.services.queue import QueueService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.scheduler import SchedulerService
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _booking(*, status=BookingStatus.COMPLETED, analysis_expiry=None, extension_minutes=15):
    equipment = Equipment.objects.create(
        code=f"QH{uuid4().hex[:6].upper()}",
        name="Queue Hygiene EQ",
        enable_remote_analysis=True,
        analysis_default_session_minutes=30,
        analysis_extension_minutes=extension_minutes,
    )
    profile = ChargeProfile.objects.create(
        equipment=equipment, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00")
    )
    return Booking.objects.create(
        user=UserFactory(),
        equipment=equipment,
        charge_profile=profile,
        status=status,
        total_time_minutes=60,
        total_charge=Decimal("10.00"),
        analysis_available=True,
        analysis_expiry=analysis_expiry if analysis_expiry is not None else timezone.now() + timedelta(hours=48),
        virtual_booking_id=f"IICQH{uuid4().hex[:8]}",
    )


def _hold(booking, workstation, status, **extra):
    now = timezone.now()
    return AnalysisReservation.objects.create(
        booking=booking,
        user=booking.user,
        workstation=workstation,
        status=status,
        requested_start=now,
        requested_end=now + timedelta(minutes=40),
        reserved_start=now if workstation else None,
        reserved_end=now + timedelta(minutes=40) if workstation else None,
        priority=100,
        **extra,
    )


def _queued(booking):
    reservation = _hold(booking, None, ReservationStatus.QUEUED)
    QueueService().enqueue(reservation)
    return reservation


def _set_ws(ws, status):
    ws.status = status
    ws.save(update_fields=["status", "updated_at"])


# --- 1. Missed check-in goes to the back of the queue -------------------------------------


def _set_enqueued_at(reservation, when):
    ReservationQueue.objects.filter(reservation=reservation).update(enqueued_at=when)


@pytest.mark.django_db
def test_missed_checkin_loses_queue_position_to_waiting_reservation(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    now = timezone.now()
    svc = ReservationService()
    first = svc.create_reservation(user=ra_user, requested_start=start, requested_end=end, created_by=ra_user)
    assert first.status == ReservationStatus.AWAITING_CHECKIN
    QueueService().enqueue(first)
    QueueService().mark_reserved(first.queue_entry)
    _set_enqueued_at(first, now - timedelta(hours=2))

    waiting = svc.create_reservation(
        user=UserFactory(), requested_start=start, requested_end=end, created_by=ra_user
    )
    assert waiting.status == ReservationStatus.QUEUED
    _set_enqueued_at(waiting, now - timedelta(hours=1))

    AnalysisReservation.objects.filter(pk=first.pk).update(checkin_expires_at=now - timedelta(seconds=1))
    CheckinService().expire_due(limit=50)

    first.refresh_from_db()
    waiting.refresh_from_db()
    assert waiting.status == ReservationStatus.AWAITING_CHECKIN
    assert waiting.workstation_id == eligible_workstation.id
    assert first.status == ReservationStatus.QUEUED
    assert first.workstation_id is None
    assert first.missed_checkin_count == 1
    first_entry = ReservationQueue.objects.get(reservation=first)
    assert first_entry.status == QueueEntryStatus.WAITING
    assert first_entry.enqueued_at > now - timedelta(minutes=1)


@pytest.mark.django_db
def test_missed_checkin_still_gets_pc_when_nobody_else_waits(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    AnalysisReservation.objects.filter(pk=reservation.pk).update(
        checkin_expires_at=timezone.now() - timedelta(seconds=1)
    )

    CheckinService().expire_due(limit=50)

    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN
    assert reservation.workstation_id == eligible_workstation.id
    assert reservation.missed_checkin_count == 1


@pytest.mark.django_db
def test_plain_requeue_keeps_queue_position(ra_user):
    now = timezone.now()
    reservation = AnalysisReservation.objects.create(
        user=ra_user,
        status=ReservationStatus.QUEUED,
        requested_start=now,
        requested_end=now + timedelta(hours=1),
        priority=100,
    )
    QueueService().enqueue(reservation)
    original = now - timedelta(hours=3)
    _set_enqueued_at(reservation, original)

    QueueService().enqueue(reservation)

    assert ReservationQueue.objects.get(reservation=reservation).enqueued_at == original


# --- 2. Ineligible bookings lose idle holds ----------------------------------------------


@pytest.mark.django_db
def test_sweep_cancels_holds_of_refunded_bookings_and_frees_pc(eligible_workstation):
    queued_booking = _booking(status=BookingStatus.REFUNDED)
    queued = _queued(queued_booking)
    held_booking = _booking(status=BookingStatus.REFUNDED)
    held = _hold(
        held_booking,
        eligible_workstation,
        ReservationStatus.AWAITING_CHECKIN,
        checkin_expires_at=timezone.now() + timedelta(minutes=5),
    )
    _set_ws(eligible_workstation, WorkstationStatus.RESERVED)

    stats = SchedulerService().expire_stale()

    assert stats["cancelled_ineligible"] == 2
    for reservation in (queued, held):
        reservation.refresh_from_db()
        assert reservation.status == ReservationStatus.CANCELLED
        history = reservation.history.order_by("-created_at").first()
        assert history.reason == "Booking no longer eligible for Remote Analysis: Booking status REFUNDED blocks analysis"
    assert ReservationQueue.objects.get(reservation=queued).status == QueueEntryStatus.CANCELLED
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE
    for booking in (queued_booking, held_booking):
        booking.refresh_from_db()
        assert booking.status == BookingStatus.REFUNDED
        assert booking.analysis_closed_at is None


@pytest.mark.django_db
def test_sweep_keeps_holds_of_eligible_bookings(eligible_workstation):
    booking = _booking()
    hold = _hold(
        booking,
        eligible_workstation,
        ReservationStatus.AWAITING_CHECKIN,
        checkin_expires_at=timezone.now() + timedelta(minutes=5),
    )
    _set_ws(eligible_workstation, WorkstationStatus.RESERVED)

    stats = SchedulerService().expire_stale()

    assert stats["cancelled_ineligible"] == 0
    hold.refresh_from_db()
    assert hold.status == ReservationStatus.AWAITING_CHECKIN
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.RESERVED


@pytest.mark.django_db
def test_sweep_never_touches_ineligible_booking_with_live_session(eligible_workstation):
    booking = _booking(status=BookingStatus.REFUNDED)
    hold = _hold(booking, eligible_workstation, ReservationStatus.RESERVED)
    RemoteDesktopSession.objects.create(
        reservation=hold,
        booking=booking,
        user=booking.user,
        workstation=eligible_workstation,
        status=SessionStatus.ACTIVE,
        expires_at=timezone.now() + timedelta(minutes=30),
    )
    _set_ws(eligible_workstation, WorkstationStatus.BUSY)

    stats = SchedulerService().expire_stale()

    assert stats["cancelled_ineligible"] == 0
    hold.refresh_from_db()
    assert hold.status == ReservationStatus.RESERVED
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.BUSY


@pytest.mark.django_db
def test_queue_processing_cancels_instead_of_allocating_refunded_booking(eligible_workstation):
    reservation = _queued(_booking(status=BookingStatus.REFUNDED))

    SchedulerService().process_queue(limit=5)

    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.CANCELLED
    assert reservation.workstation_id is None
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE


@pytest.mark.django_db
def test_booking_refund_cancels_idle_hold_after_commit(eligible_workstation, django_capture_on_commit_callbacks):
    booking = _booking()
    hold = _hold(
        booking,
        eligible_workstation,
        ReservationStatus.AWAITING_CHECKIN,
        checkin_expires_at=timezone.now() + timedelta(minutes=5),
    )
    _set_ws(eligible_workstation, WorkstationStatus.RESERVED)

    with django_capture_on_commit_callbacks(execute=True):
        booking.status = BookingStatus.REFUNDED
        booking.save(update_fields=["status", "updated_at"])

    hold.refresh_from_db()
    assert hold.status == ReservationStatus.CANCELLED
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE
    booking.refresh_from_db()
    assert booking.status == BookingStatus.REFUNDED


@pytest.mark.django_db
def test_sweep_eligibility_check_skips_lazy_booking_close(monkeypatch):
    calls = []
    monkeypatch.setattr(session_close, "ensure_analysis_closed_from_history", lambda b: calls.append(b) or False)
    booking = _booking()

    assert BookingAnalysisEligibilityService().evaluate(booking, close_from_history=False).eligible
    assert calls == []
    BookingAnalysisEligibilityService().evaluate(booking)
    assert calls == [booking]


# --- 3. Extending a session extends its reservation --------------------------------------


def _live_session(booking, workstation, *, remaining_minutes=10):
    now = timezone.now()
    end = now + timedelta(minutes=remaining_minutes)
    reservation = AnalysisReservation.objects.create(
        booking=booking,
        user=booking.user,
        workstation=workstation,
        status=ReservationStatus.ACTIVE,
        requested_start=now - timedelta(minutes=20),
        requested_end=end,
        reserved_start=now - timedelta(minutes=20),
        reserved_end=end,
        priority=100,
    )
    session = RemoteDesktopSession.objects.create(
        reservation=reservation,
        booking=booking,
        user=booking.user,
        workstation=workstation,
        status=SessionStatus.ACTIVE,
        expires_at=end,
    )
    _set_ws(workstation, WorkstationStatus.BUSY)
    return reservation, session


@pytest.mark.django_db
def test_extend_session_extends_reservation(eligible_workstation):
    booking = _booking()
    reservation, session = _live_session(booking, eligible_workstation)
    old_end = session.expires_at

    payload = BookingRemoteAnalysisService().extend_analysis(booking, user=booking.user)

    session.refresh_from_db()
    reservation.refresh_from_db()
    assert payload["extended_minutes"] == 15
    assert session.expires_at == old_end + timedelta(minutes=15)
    assert reservation.reserved_end == session.expires_at
    assert reservation.requested_end == session.expires_at

    SchedulerService().expire_stale()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.ACTIVE


@pytest.mark.django_db
def test_extend_session_is_capped_at_analysis_expiry(eligible_workstation):
    expiry = timezone.now() + timedelta(minutes=17)
    booking = _booking(analysis_expiry=expiry)
    reservation, session = _live_session(booking, eligible_workstation)

    payload = BookingRemoteAnalysisService().extend_analysis(booking, user=booking.user)

    session.refresh_from_db()
    reservation.refresh_from_db()
    assert session.expires_at == expiry
    assert reservation.reserved_end == expiry
    assert payload["extended_minutes"] < 15

    with pytest.raises(SessionError) as exc:
        BookingRemoteAnalysisService().extend_analysis(booking, user=booking.user)
    assert exc.value.code == "analysis_expired"


@pytest.mark.django_db
def test_extend_session_refused_when_next_reservation_holds_pc(eligible_workstation):
    booking = _booking()
    reservation, session = _live_session(booking, eligible_workstation)
    old_end = session.expires_at
    next_booking = _booking()
    AnalysisReservation.objects.create(
        booking=next_booking,
        user=next_booking.user,
        workstation=eligible_workstation,
        status=ReservationStatus.RESERVED,
        requested_start=old_end + timedelta(minutes=5),
        requested_end=old_end + timedelta(minutes=45),
        reserved_start=old_end + timedelta(minutes=5),
        reserved_end=old_end + timedelta(minutes=45),
        priority=100,
    )

    with pytest.raises(SessionError) as exc:
        BookingRemoteAnalysisService().extend_analysis(booking, user=booking.user)

    assert exc.value.code == "extension_conflict"
    session.refresh_from_db()
    reservation.refresh_from_db()
    assert session.expires_at == old_end
    assert reservation.reserved_end == old_end
