"""scripts/ops/backfill_disruption_history.py: missing disruption entries and decision reasons."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from iic_booking.equipment.disruption_models import SlotStatusChangeLog
from iic_booking.equipment.models import (
    BookingDisruptionKind,
    BookingEvent,
    BookingEventType,
    BookingSlotRange,
    BookingStatus,
    EquipmentManager,
)
from iic_booking.equipment.serializers import BookingEventSerializer
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ops" / "backfill_disruption_history.py"


def _run(monkeypatch, apply=True):
    monkeypatch.setenv("APPLY", "1" if apply else "0")
    exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), {"__name__": "backfill"})


def _audit(event):
    return BookingEventSerializer(event).data["audit"]


@pytest.fixture
def lab():
    f = _EgsFactory()
    eq = f.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return f, eq, oic


def test_pending_booking_gets_the_oic_entry_from_the_slot_log(lab, monkeypatch):
    f, eq, oic = lab
    student = f.student()
    booking = f.booking(student, eq, f.future(days=1))
    BookingEvent.objects.filter(booking=booking).delete()
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.maintenance_disruption_flag = True
    booking.save()
    marked = timezone.now() - timedelta(days=2)
    SlotStatusChangeLog.objects.create(
        equipment=eq,
        new_status="OPERATOR_ABSENT",
        slot_ids=list(booking.daily_slots.values_list("id", flat=True)),
        changed_by=oic,
        changed_at=marked,
    )

    _run(monkeypatch)
    _run(monkeypatch)

    (event,) = BookingEvent.objects.filter(booking=booking, new_status=BookingStatus.DISRUPTION_PENDING)
    assert event.created_by == oic
    assert abs(event.created_at - marked) < timedelta(seconds=1)
    audit = _audit(event)
    assert (audit["actor_role"], audit["reason_code"]) == ("OIC", "OPERATOR_ABSENT")
    assert audit["source_label"] == "Recorded later from the slot status log"


def test_refunded_booking_gets_an_entry_before_the_decision_and_its_reason(lab, monkeypatch):
    f, eq, _oic = lab
    student = f.student()
    booking = f.booking(student, eq, f.future(days=-3))
    start = booking.daily_slots.first().start_datetime
    BookingSlotRange.objects.create(booking=booking, start_datetime=start, end_datetime=start + timedelta(hours=1))
    booking.daily_slots.update(booking=None)
    booking.status = BookingStatus.ABSENT
    booking.notes = "[Operator Unavailable]: Operator on leave"
    booking.save()
    BookingEvent.objects.filter(booking=booking).delete()
    decision = BookingEvent.objects.create(
        booking=booking,
        event_type=BookingEventType.ABSENT,
        previous_status=BookingStatus.DISRUPTION_PENDING,
        new_status=BookingStatus.ABSENT,
        created_by=student,
        comment="Booking cancelled and refunded by user.",
    )

    _run(monkeypatch, apply=False)
    assert BookingEvent.objects.filter(booking=booking).count() == 1

    _run(monkeypatch)

    entry = BookingEvent.objects.get(booking=booking, new_status=BookingStatus.DISRUPTION_PENDING)
    assert entry.created_at < decision.created_at
    audit = _audit(entry)
    assert (audit["reason_code"], audit["reason"]) == ("OPERATOR_ABSENT", "Operator on leave")
    assert audit["actor_role_label"] == "Staff (name not recorded)"
    decision.refresh_from_db()
    assert _audit(decision)["reason"] == "User chose a full refund after Operator Absent"
