"""Cancellation records: who cancelled, why, how late and what was refunded; restore; backfill from history."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.booking_cancellation import perform_booking_cancellation
from iic_booking.equipment.booking_events import create_booking_event
from iic_booking.equipment.models import (
    BookingCancellation,
    BookingCancellationRequest,
    BookingEvent,
    BookingEventType,
    BookingStatus,
    CancellationActorRole,
    CancellationDataQuality,
    CancellationReason,
)
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def lab():
    f = _EgsFactory()
    eq = f.equipment()
    student = f.student()
    booking = f.booking(student, eq, f.future(days=3), total_charge="100.00", slot_count=2)
    return f, eq, student, booking


def _cancel(booking, actor, *, label="user", notes=""):
    slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    perform_booking_cancellation(
        booking,
        slot_ids=slot_ids,
        should_refund=False,
        cancel_notes=notes,
        actor=actor,
        allow_started_slots=True,
        cancelled_by_label=label,
    )
    return slot_ids


def test_user_cancellation_is_recorded(lab):
    _f, _eq, student, booking = lab
    slot_ids = _cancel(booking, student, notes="Plans changed")
    row = BookingCancellation.objects.get(booking=booking)
    assert row.actor_role == CancellationActorRole.USER
    assert row.cancelled_by_id == student.pk
    assert row.reason == CancellationReason.USER_REQUEST
    assert row.note == "Plans changed"
    assert row.previous_status == BookingStatus.BOOKED
    assert row.new_status == BookingStatus.CANCELLED
    assert row.charge_amount == Decimal("100.00")
    assert row.refund_amount == Decimal("0.00")
    assert sorted(row.released_slot_ids) == sorted(slot_ids)
    assert row.slot_start is not None and row.lead_minutes > 24 * 60
    assert row.data_quality == CancellationDataQuality.RECORDED


@pytest.mark.parametrize(
    "user_type,label,role,reason",
    [
        (UserType.ADMIN, "admin", CancellationActorRole.MAIN_ADMIN, CancellationReason.STAFF_CANCEL),
        (UserType.DEPT_ADMIN, "admin", CancellationActorRole.DEPT_ADMIN, CancellationReason.STAFF_CANCEL),
        (UserType.MANAGER, "admin", CancellationActorRole.OIC, CancellationReason.STAFF_CANCEL),
        (UserType.OPERATOR, "admin", CancellationActorRole.LAB_OPERATOR, CancellationReason.STAFF_CANCEL),
    ],
)
def test_staff_cancellations_name_the_role(lab, user_type, label, role, reason):
    _f, _eq, _student, booking = lab
    _cancel(booking, UserFactory(user_type=user_type), label=label)
    row = BookingCancellation.objects.get(booking=booking)
    assert (row.actor_role, row.reason) == (role, reason)


def test_supervisor_cancellation(lab):
    _f, _eq, student, booking = lab
    faculty = UserFactory(user_type=UserType.FACULTY)
    student.supervisor = faculty
    student.save(update_fields=["supervisor"])
    _cancel(booking, faculty)
    row = BookingCancellation.objects.get(booking=booking)
    assert (row.actor_role, row.reason) == (CancellationActorRole.SUPERVISOR, CancellationReason.USER_REQUEST)


def test_system_cancellation(lab):
    _f, _eq, _student, booking = lab
    _cancel(booking, None, label="system", notes="Lab rejected the files")
    row = BookingCancellation.objects.get(booking=booking)
    assert row.actor_role == CancellationActorRole.SYSTEM
    assert row.cancelled_by_id is None


def test_lab_disruption_refund_is_estimated_when_not_recorded(lab):
    _f, _eq, _student, booking = lab
    manager = UserFactory(user_type=UserType.MANAGER)
    booking.status = BookingStatus.UNDER_MAINTENANCE
    booking.save(update_fields=["status"])
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.STATUS_CHANGED,
        previous_status=BookingStatus.BOOKED,
        new_status=BookingStatus.UNDER_MAINTENANCE,
        created_by=manager,
        send_notification=False,
    )
    row = BookingCancellation.objects.get(booking=booking)
    assert row.reason == CancellationReason.EQUIPMENT_DOWN
    assert row.actor_role == CancellationActorRole.OIC
    assert (row.refund_amount, row.refund_estimated) == (Decimal("100.00"), True)


def test_refund_after_cancel_updates_the_same_row(lab):
    _f, _eq, student, booking = lab
    _cancel(booking, student)
    admin = UserFactory(user_type=UserType.ADMIN)
    booking.status = BookingStatus.REFUNDED
    booking.save(update_fields=["status"])
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.REFUNDED,
        previous_status=BookingStatus.CANCELLED,
        new_status=BookingStatus.REFUNDED,
        created_by=admin,
        send_notification=False,
        cancellation={"actor": admin, "refund_amount": Decimal("60.00")},
    )
    row = BookingCancellation.objects.get(booking=booking)
    assert row.actor_role == CancellationActorRole.USER
    assert row.new_status == BookingStatus.REFUNDED
    assert (row.refund_amount, row.refund_estimated) == (Decimal("60.00"), False)


def test_restoring_a_booking_drops_the_row(lab):
    _f, _eq, student, booking = lab
    _cancel(booking, student)
    assert BookingCancellation.objects.filter(booking=booking).exists()
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.STATUS_CHANGED,
        previous_status=BookingStatus.CANCELLED,
        new_status=BookingStatus.BOOKED,
        created_by=UserFactory(user_type=UserType.ADMIN),
        send_notification=False,
    )
    assert not BookingCancellation.objects.filter(booking=booking).exists()


def test_recording_failure_never_blocks_the_cancellation(lab, monkeypatch):
    from iic_booking.equipment import booking_cancellation_log

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(booking_cancellation_log, "record_cancellation", boom)
    _f, _eq, student, booking = lab
    _cancel(booking, student)
    booking.refresh_from_db()
    assert booking.status == BookingStatus.CANCELLED
    assert not BookingCancellation.objects.filter(booking=booking).exists()


def test_approved_cancellation_request_is_recorded(lab):
    _f, _eq, student, booking = lab
    request = BookingCancellationRequest.objects.create(booking=booking, user=student, notes="Sample not ready")
    request.approve()
    row = BookingCancellation.objects.get(booking=booking)
    assert row.reason == CancellationReason.CANCELLATION_REQUEST
    assert row.actor_role == CancellationActorRole.USER
    assert row.note == "Sample not ready"
    assert row.released_slot_ids


@pytest.mark.parametrize("with_actor", [True, False])
def test_urgent_hold_release(lab, with_actor, monkeypatch):
    from iic_booking.equipment import api_views

    monkeypatch.setattr(api_views, "notify_waitlist_slots_available", lambda *a, **k: 0)
    _f, _eq, _student, booking = lab
    booking.status = BookingStatus.HOLD
    booking.save(update_fields=["status"])
    oic = UserFactory(user_type=UserType.MANAGER)
    api_views._release_hold_booking(booking, actor=oic if with_actor else None)
    row = BookingCancellation.objects.get(booking=booking)
    assert row.reason == CancellationReason.URGENT_HOLD_RELEASED
    assert row.actor_role == (CancellationActorRole.OIC if with_actor else CancellationActorRole.SYSTEM)
    assert row.charge_amount == Decimal("0.00")


# --------------------------------------------------------------------------- backfill


def _old_event(booking, *, previous, new, by, comment="", event_type=BookingEventType.CANCELLED, **metadata):
    """An event written before cancellations were recorded (the hook is bypassed)."""
    return BookingEvent.objects.create(
        booking=booking,
        event_type=event_type,
        previous_status=previous,
        new_status=new,
        comment=comment,
        created_by=by,
        metadata=metadata,
    )


def _set_status(booking, status):
    booking.status = status
    booking.save(update_fields=["status"])


def test_backfill_from_history_requests_and_status(lab):
    from iic_booking.equipment.management.commands.backfill_booking_cancellations import (
        backfill_booking_cancellations,
    )

    f, eq, student, from_events = lab
    _set_status(from_events, BookingStatus.CANCELLED)
    _old_event(from_events, previous="BOOKED", new="CANCELLED", by=student, comment="Booking cancelled by user. Clash")

    automatic = f.booking(student, eq, f.future(days=4), total_charge="80.00")
    _set_status(automatic, BookingStatus.REFUNDED)
    _old_event(
        automatic,
        previous="DISRUPTION_PENDING",
        new="REFUNDED",
        by=student,
        comment="Auto-cancelled: maintenance disruption policy deadline.",
        refund_amount="80.00",
    )

    by_request = f.booking(student, eq, f.future(days=5), total_charge="30.00")
    _set_status(by_request, BookingStatus.CANCELLED)
    BookingCancellationRequest.objects.create(
        booking=by_request, user=student, status="APPROVED", responded_at=timezone.now() - timedelta(days=1)
    )

    refunded_only = f.booking(student, eq, f.future(days=6), total_charge="50.00")
    _set_status(refunded_only, BookingStatus.REFUNDED)
    cancelled_only = f.booking(student, eq, f.future(days=7), total_charge="20.00")
    _set_status(cancelled_only, BookingStatus.CANCELLED)
    BookingCancellation.objects.all().delete()

    dry = backfill_booking_cancellations(apply=False)
    assert dry["bookings_scanned"] == 5
    assert not BookingCancellation.objects.exists()

    result = backfill_booking_cancellations(apply=True)
    assert (result["from_history"], result["from_request"], result["inferred"], result["failed"]) == (2, 1, 2, 0)
    rows = {r.booking_id: r for r in BookingCancellation.objects.all()}

    hist = rows[from_events.pk]
    assert (hist.actor_role, hist.reason, hist.data_quality) == (
        CancellationActorRole.USER,
        CancellationReason.USER_REQUEST,
        CancellationDataQuality.FROM_HISTORY,
    )
    assert hist.note == "Clash"

    auto = rows[automatic.pk]
    assert (auto.actor_role, auto.reason) == (CancellationActorRole.SYSTEM, CancellationReason.DISRUPTION_DEADLINE)
    assert (auto.refund_amount, auto.refund_estimated) == (Decimal("80.00"), False)

    assert rows[by_request.pk].reason == CancellationReason.CANCELLATION_REQUEST

    inferred = rows[refunded_only.pk]
    assert inferred.data_quality == CancellationDataQuality.INFERRED
    assert inferred.actor_role == CancellationActorRole.UNKNOWN
    assert (inferred.refund_amount, inferred.refund_estimated) == (Decimal("50.00"), True)
    assert rows[cancelled_only.pk].refund_amount is None

    again = backfill_booking_cancellations(apply=True)
    assert again["bookings_scanned"] == 0


def test_backfill_keeps_recorded_rows_on_rebuild(lab):
    from iic_booking.equipment.management.commands.backfill_booking_cancellations import (
        backfill_booking_cancellations,
    )

    _f, _eq, student, booking = lab
    _cancel(booking, student, notes="Recorded")
    result = backfill_booking_cancellations(apply=True, rebuild=True)
    assert result["removed_for_rebuild"] == 0
    assert BookingCancellation.objects.get(booking=booking).data_quality == CancellationDataQuality.RECORDED


def test_backfill_command_runs(lab):
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("backfill_booking_cancellations", stdout=out)
    assert "mode=DRY RUN" in out.getvalue()
