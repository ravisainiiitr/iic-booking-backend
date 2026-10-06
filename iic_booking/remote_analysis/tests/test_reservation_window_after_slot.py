"""Regression: Analyze Data relaunch after the booked slot ended (IICDSATEST202600018).

The reservation used to inherit the past slot window, so the session gate rejected it with
"Reservation / analysis window has ended" while booking.analysis_expiry was days away.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, DailySlot, Equipment, SlotMaster
from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService
from iic_booking.remote_analysis.constants import ConflictType, ReservationStatus, WorkstationStatus
from iic_booking.remote_analysis.guacamole.authorization import evaluate_session_create_gates
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.checkin import CheckinService
from iic_booking.remote_analysis.services.conflicts import ConflictResolver
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

SESSION_MINUTES = 30
CHECKIN_MINUTES = 10


def _booking(slot_start, slot_end, *, analysis_expiry=None, equipment=None):
    if equipment is None:
        equipment = Equipment.objects.create(
            code=f"RW{uuid4().hex[:6].upper()}",
            name="Reservation Window EQ",
            enable_remote_analysis=True,
            analysis_default_session_minutes=SESSION_MINUTES,
            analysis_checkin_minutes=CHECKIN_MINUTES,
        )
    profile = ChargeProfile.objects.create(
        equipment=equipment, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00")
    )
    booking = Booking.objects.create(
        user=UserFactory(),
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        total_time_minutes=60,
        total_charge=Decimal("10.00"),
        analysis_available=True,
        analysis_available_from=slot_start,
        analysis_expiry=analysis_expiry,
        virtual_booking_id=f"IICRW{uuid4().hex[:8]}",
    )
    slot_master = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=SlotMaster.objects.filter(equipment=equipment).count() + 1,
        open_time=slot_start.time().replace(microsecond=0),
        close_time=slot_end.time().replace(microsecond=0),
        is_active=True,
    )
    DailySlot.objects.create(
        slot_master=slot_master,
        date=slot_start.date(),
        start_datetime=slot_start,
        end_datetime=slot_end,
        status="BOOKED",
        booking=booking,
    )
    return booking


def _completed_first_reservation(booking, slot_start, slot_end):
    return AnalysisReservation.objects.create(
        booking=booking,
        user=booking.user,
        status=ReservationStatus.COMPLETED,
        requested_start=slot_start,
        requested_end=slot_end,
        reserved_start=slot_start,
        reserved_end=slot_end,
        released_at=slot_start + timedelta(minutes=20),
        priority=100,
    )


@pytest.mark.django_db
def test_relaunch_after_slot_end_gets_live_window_and_passes_gate(eligible_workstation):
    now = timezone.now()
    slot_start, slot_end = now - timedelta(hours=8), now - timedelta(hours=7)
    booking = _booking(slot_start, slot_end, analysis_expiry=now + timedelta(hours=60))
    _completed_first_reservation(booking, slot_start, slot_end)

    reservation = BookingRemoteAnalysisService().ensure_reservation(booking, actor=booking.user)

    assert reservation.status == ReservationStatus.AWAITING_CHECKIN
    assert reservation.workstation_id == eligible_workstation.id
    assert abs(reservation.requested_start - now) < timedelta(minutes=1)
    assert reservation.requested_end - reservation.requested_start == timedelta(
        minutes=CHECKIN_MINUTES + SESSION_MINUTES
    )
    assert reservation.reserved_end == reservation.requested_end
    gate = evaluate_session_create_gates(reservation=reservation, user=booking.user)
    assert gate.ok, gate.reason
    assert gate.checks["analysis_window_open"] is True


@pytest.mark.django_db
def test_post_slot_window_is_capped_at_analysis_expiry(eligible_workstation):
    now = timezone.now()
    expiry = now + timedelta(minutes=12)
    booking = _booking(now - timedelta(hours=3), now - timedelta(hours=2), analysis_expiry=expiry)

    reservation = BookingRemoteAnalysisService().ensure_reservation(booking, actor=booking.user)

    assert reservation.requested_end == expiry


@pytest.mark.django_db
def test_relaunch_after_analysis_expiry_is_still_rejected(eligible_workstation):
    now = timezone.now()
    slot_start, slot_end = now - timedelta(days=4), now - timedelta(days=4) + timedelta(hours=1)
    booking = _booking(slot_start, slot_end, analysis_expiry=now - timedelta(hours=1))

    with pytest.raises(ValueError, match="expired"):
        BookingRemoteAnalysisService().ensure_reservation(booking, actor=booking.user)
    with pytest.raises(ValueError, match="has ended"):
        ReservationService().create_reservation(
            user=booking.user,
            requested_start=now,
            requested_end=now + timedelta(hours=72),
            booking=booking,
            auto_allocate=False,
        )
    assert not AnalysisReservation.objects.filter(booking=booking).exists()

    stale = AnalysisReservation.objects.create(
        booking=booking,
        user=booking.user,
        workstation=eligible_workstation,
        status=ReservationStatus.AWAITING_CHECKIN,
        requested_start=slot_start,
        requested_end=slot_end,
        priority=100,
    )
    gate = evaluate_session_create_gates(reservation=stale, user=booking.user)
    assert gate.ok is False and gate.code == "reservation_expired"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "offsets",
    [
        (timedelta(hours=1), timedelta(hours=2)),
        (timedelta(minutes=-30), timedelta(minutes=30)),
    ],
    ids=["before-slot", "during-slot"],
)
def test_reservation_before_or_during_slot_keeps_slot_window(eligible_workstation, offsets):
    now = timezone.now()
    slot_start, slot_end = now + offsets[0], now + offsets[1]
    booking = _booking(slot_start, slot_end, analysis_expiry=now + timedelta(hours=72))

    reservation = BookingRemoteAnalysisService().ensure_reservation(booking, actor=booking.user)

    assert reservation.requested_start == slot_start
    assert reservation.requested_end == slot_end


@pytest.mark.django_db
def test_post_slot_hold_does_not_block_other_bookings_on_same_pc(eligible_workstation):
    now = timezone.now()
    booking = _booking(now - timedelta(hours=8), now - timedelta(hours=7), analysis_expiry=now + timedelta(hours=60))
    reservation = BookingRemoteAnalysisService().ensure_reservation(booking, actor=booking.user)
    assert reservation.workstation_id == eligible_workstation.id
    ReservationService().transition(reservation, ReservationStatus.ACTIVE, reason="connected")

    later_start = now + timedelta(hours=3)
    later_end = later_start + timedelta(hours=1)
    assert not AvailabilityEngine().has_reservation_overlap(eligible_workstation, later_start, later_end)
    conflicts = ConflictResolver().detect_for_window(eligible_workstation, later_start, later_end)
    assert ConflictType.DOUBLE_BOOKING not in {c.conflict_type for c in conflicts}

    # The live session itself is still protected.
    assert AvailabilityEngine().has_reservation_overlap(eligible_workstation, now, now + timedelta(minutes=5))


@pytest.mark.django_db
def test_stale_slot_window_hold_is_retired_on_missed_checkin(eligible_workstation):
    """A pre-fix reservation stuck in the check-in loop expires instead of re-holding the PC."""
    now = timezone.now()
    slot_start, slot_end = now - timedelta(hours=8), now - timedelta(hours=7)
    booking = _booking(slot_start, slot_end, analysis_expiry=now + timedelta(hours=60))
    _completed_first_reservation(booking, slot_start, slot_end)
    stale = AnalysisReservation.objects.create(
        booking=booking,
        user=booking.user,
        workstation=eligible_workstation,
        status=ReservationStatus.AWAITING_CHECKIN,
        requested_start=slot_start,
        requested_end=slot_end,
        reserved_start=slot_start,
        reserved_end=slot_end,
        checkin_expires_at=now - timedelta(minutes=1),
        priority=100,
    )
    booking.analysis_reservation = stale
    booking.save(update_fields=["analysis_reservation", "updated_at"])
    eligible_workstation.status = WorkstationStatus.RESERVED
    eligible_workstation.save(update_fields=["status", "updated_at"])

    CheckinService().expire_due(limit=50)

    stale.refresh_from_db()
    assert stale.status == ReservationStatus.EXPIRED
    assert stale.workstation_id is None
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE

    fresh = BookingRemoteAnalysisService().ensure_reservation(booking, actor=booking.user)
    assert fresh.pk != stale.pk
    assert fresh.requested_end > timezone.now()
    assert evaluate_session_create_gates(reservation=fresh, user=booking.user).ok
