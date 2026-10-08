"""A booking made on Reserved (External) slots gives them back as Reserved (External), with their I-STEM FBR
reference, whenever it releases them (cancel, partial cancel, reschedule away, refunds, holds, auto-cancel)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from iic_booking.equipment import waitlist
from iic_booking.equipment.booking_cancellation import parse_cancellation_request, perform_booking_cancellation
from iic_booking.equipment.models import (
    Booking,
    BookingCancellationRequest,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    SlotStatus,
    WaitlistEntry,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

FBR = "FBR-2026-0042"


def _admin(egs_factory):
    return UserFactory(
        user_type=UserType.ADMIN, is_staff=True, admin_approved=True, department=egs_factory.department
    )


def _reserved_slots(egs_factory, eq, start, count=1, reference=FBR):
    slots = [
        egs_factory.slot(eq, start + timedelta(hours=i), status=SlotStatus.RESERVED_EXTERNAL) for i in range(count)
    ]
    DailySlot.objects.filter(pk__in=[s.pk for s in slots]).update(external_reference=reference)
    return slots


def _book_slots(egs_factory, owner, eq, slots):
    """Take the slots the way every booking path does: one queryset update to BOOKED."""
    booking = Booking.objects.create(
        user=owner,
        equipment=eq,
        charge_profile=ChargeProfile.objects.filter(equipment=eq).first(),
        status=BookingStatus.BOOKED,
        total_charge=Decimal("10.00") * len(slots),
        total_time_minutes=60 * len(slots),
        input_values={},
        virtual_booking_id=f"IIC{eq.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )
    DailySlot.objects.filter(pk__in=[s.pk for s in slots]).update(booking=booking, status=SlotStatus.BOOKED)
    return booking


def _state(slot):
    slot.refresh_from_db()
    return slot.status, slot.external_reference, slot.booking_id


def _setup(egs_factory, count=1, reference=FBR):
    eq = egs_factory.equipment()
    owner = egs_factory.student()
    slots = _reserved_slots(egs_factory, eq, egs_factory.future(days=5, hour=9), count, reference)
    booking = _book_slots(egs_factory, owner, eq, slots)
    return SimpleNamespace(eq=eq, owner=owner, slots=slots, booking=booking)


def test_booking_keeps_reference_and_full_cancel_restores_reserved(egs_factory):
    w = _setup(egs_factory, count=2)
    assert {_state(s) for s in w.slots} == {(SlotStatus.BOOKED, FBR, w.booking.pk)}

    perform_booking_cancellation(
        w.booking,
        slot_ids=[s.pk for s in w.slots],
        should_refund=False,
        cancel_notes="",
        actor=_admin(egs_factory),
        allow_started_slots=True,
    )

    w.booking.refresh_from_db()
    assert w.booking.status == BookingStatus.CANCELLED
    assert {_state(s) for s in w.slots} == {(SlotStatus.RESERVED_EXTERNAL, FBR, None)}


def test_reserved_slot_without_reference_is_restored_without_reference(egs_factory):
    w = _setup(egs_factory, reference=None)
    assert _state(w.slots[0]) == (SlotStatus.BOOKED, "", w.booking.pk)

    w.booking.daily_slots.update(booking=None, status=SlotStatus.AVAILABLE)

    status, reference, booking_id = _state(w.slots[0])
    assert (status, booking_id) == (SlotStatus.RESERVED_EXTERNAL, None)
    assert not reference


def test_partial_cancel_restores_only_released_reserved_slot(egs_factory):
    w = _setup(egs_factory, count=2)
    first, second = w.slots
    parsed = parse_cancellation_request({"slot_ids": [second.pk]}, w.booking)
    assert parsed["mode"] == "partial_slots"

    perform_booking_cancellation(
        w.booking,
        slot_ids=parsed["slot_ids"],
        should_refund=False,
        cancel_notes="",
        actor=_admin(egs_factory),
        allow_started_slots=True,
        partial_plan=parsed["plan"],
    )

    assert _state(first) == (SlotStatus.BOOKED, FBR, w.booking.pk)
    assert _state(second) == (SlotStatus.RESERVED_EXTERNAL, FBR, None)


@pytest.mark.parametrize("endpoint", ["reschedule", "user-reschedule"])
def test_reschedule_away_restores_reserved_and_new_slot_is_plain(egs_factory, egs_quiet_side_effects, endpoint):
    w = _setup(egs_factory)
    new_slot = egs_factory.slot(w.eq, egs_factory.future(days=6, hour=11))
    actor = _admin(egs_factory) if endpoint == "reschedule" else w.owner

    res = egs_factory.client_for(actor).post(
        f"/api/bookings/{w.booking.pk}/{endpoint}/",
        {"start_time": new_slot.start_datetime.isoformat(), "end_time": new_slot.end_datetime.isoformat()},
        format="json",
    )

    assert res.status_code == 200, res.data
    assert _state(w.slots[0]) == (SlotStatus.RESERVED_EXTERNAL, FBR, None)
    assert _state(new_slot) == (SlotStatus.BOOKED, None, w.booking.pk)

    # Cancelling the moved booking frees the ordinary slot as Available.
    w.booking.daily_slots.update(booking=None, status=SlotStatus.AVAILABLE)
    assert _state(new_slot) == (SlotStatus.AVAILABLE, None, None)


def test_release_by_booking_id_and_cancellation_request_paths(egs_factory):
    w = _setup(egs_factory)
    DailySlot.objects.filter(booking=w.booking).update(booking_id=None, status=SlotStatus.AVAILABLE)
    assert _state(w.slots[0]) == (SlotStatus.RESERVED_EXTERNAL, FBR, None)

    w2 = _setup(egs_factory)
    request = BookingCancellationRequest.objects.create(booking=w2.booking, user=w2.owner, notes="Not needed")
    request.approve()
    assert _state(w2.slots[0]) == (SlotStatus.RESERVED_EXTERNAL, FBR, None)


def test_handover_between_bookings_keeps_reserved_memory(egs_factory):
    w = _setup(egs_factory)
    other = _book_slots(egs_factory, w.owner, w.eq, [])
    DailySlot.objects.filter(pk=w.slots[0].pk).update(booking=other, status=SlotStatus.BOOKED)
    assert _state(w.slots[0]) == (SlotStatus.BOOKED, FBR, other.pk)

    other.daily_slots.update(booking=None, status=SlotStatus.AVAILABLE)
    assert _state(w.slots[0]) == (SlotStatus.RESERVED_EXTERNAL, FBR, None)


def test_ordinary_slot_is_released_available_and_stale_reference_cleared(egs_factory):
    eq = egs_factory.equipment()
    slot = egs_factory.slot(eq, egs_factory.future(days=4, hour=10))
    DailySlot.objects.filter(pk=slot.pk).update(external_reference="OLD-REF")

    booking = _book_slots(egs_factory, egs_factory.student(), eq, [slot])
    assert _state(slot) == (SlotStatus.BOOKED, None, booking.pk)

    booking.daily_slots.update(booking=None, status=SlotStatus.AVAILABLE)
    assert _state(slot) == (SlotStatus.AVAILABLE, None, None)


def test_release_to_maintenance_or_explicit_staff_status_is_not_overridden(egs_factory):
    w = _setup(egs_factory)
    w.booking.daily_slots.update(booking=None, status=SlotStatus.UNDER_MAINTENANCE)
    assert _state(w.slots[0]) == (SlotStatus.UNDER_MAINTENANCE, FBR, None)

    w2 = _setup(egs_factory)
    w2.booking.daily_slots.update(booking=None, status=SlotStatus.AVAILABLE, external_reference=None)
    assert _state(w2.slots[0]) == (SlotStatus.AVAILABLE, None, None)


# --- waitlist ----------------------------------------------------------------------------------


@pytest.fixture
def waitlist_spy(monkeypatch):
    calls = SimpleNamespace(booked=[], short_notice=[])

    def _book(equipment, user, slot_ids, **kwargs):
        calls.booked.append(list(slot_ids))
        return None, "spy"

    def _short_notice(equipment, **kwargs):
        calls.short_notice.append(equipment.pk)
        return 0

    monkeypatch.setattr(waitlist, "create_booking_for_waitlist_user", _book)
    monkeypatch.setattr(
        waitlist, "reduce_waitlist_inputs_to_fit_available_slots", lambda *a, **k: ({}, 60, 1)
    )
    monkeypatch.setattr(waitlist, "_notify_waitlist_short_notice_slot_available", _short_notice)
    return calls


def _cancel(egs_factory, booking):
    perform_booking_cancellation(
        booking,
        slot_ids=list(booking.daily_slots.values_list("id", flat=True)),
        should_refund=False,
        cancel_notes="",
        actor=_admin(egs_factory),
        allow_started_slots=True,
    )


def test_waitlist_does_not_take_released_reserved_slot(egs_factory, waitlist_spy):
    w = _setup(egs_factory)
    WaitlistEntry.objects.create(equipment=w.eq, user=egs_factory.student(), status="ACTIVE")
    _cancel(egs_factory, w.booking)

    assert waitlist.notify_waitlist_slots_available(w.eq, preferred_slot_ids=[w.slots[0].pk]) == 0
    assert waitlist.notify_waitlist_slots_available(w.eq) == 0
    assert waitlist_spy.booked == []
    assert _state(w.slots[0]) == (SlotStatus.RESERVED_EXTERNAL, FBR, None)


def test_waitlist_short_notice_email_skipped_for_reserved_slot(egs_factory, waitlist_spy):
    w = _setup(egs_factory)
    w.eq.reschedule_hours_threshold = 24 * 30
    w.eq.save(update_fields=["reschedule_hours_threshold"])
    WaitlistEntry.objects.create(equipment=w.eq, user=egs_factory.student(), status="ACTIVE")
    _cancel(egs_factory, w.booking)

    waitlist.notify_waitlist_slots_available(
        w.eq, preferred_slot_ids=[w.slots[0].pk], respect_reschedule_threshold=True
    )
    assert waitlist_spy.short_notice == []


def test_waitlist_still_takes_released_ordinary_slot(egs_factory, waitlist_spy):
    eq = egs_factory.equipment()
    slot = egs_factory.slot(eq, egs_factory.future(days=5, hour=9))
    booking = _book_slots(egs_factory, egs_factory.student(), eq, [slot])
    WaitlistEntry.objects.create(equipment=eq, user=egs_factory.student(), status="ACTIVE")
    _cancel(egs_factory, booking)

    waitlist.notify_waitlist_slots_available(eq, preferred_slot_ids=[slot.pk])
    assert waitlist_spy.booked == [[slot.pk]]


# --- end to end: OIC/Admin "Book slots for a user" on a Reserved (External) slot ---------------------


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def test_admin_books_reserved_slot_for_user_then_admin_cancel_restores_it(egs_factory, monkeypatch, no_portal_lock):
    from iic_booking.equipment import api_views
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
    from iic_booking.users.repositories.wallet_repository import SubWalletRepository

    eq = egs_factory.equipment()
    student = egs_factory.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    SubWalletRepository.get_or_create(wallet, egs_factory.department).credit(
        Decimal("1000.00"), description="Recharge"
    )
    (slot,) = _reserved_slots(egs_factory, eq, egs_factory.future(days=3, hour=10))
    admin = _admin(egs_factory)
    monkeypatch.setattr(api_views, "_actor_may_book_on_behalf", lambda actor, equipment: None)
    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
    }

    assert egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", body, format="json").status_code >= 400
    assert _state(slot) == (SlotStatus.RESERVED_EXTERNAL, FBR, None)

    res = egs_factory.client_for(admin).post(
        f"/api/equipments/{eq.pk}/book/", {**body, "user_id": student.pk}, format="json"
    )
    assert res.status_code in (200, 201), res.data
    booking = Booking.objects.get(user=student)
    assert _state(slot) == (SlotStatus.BOOKED, FBR, booking.pk)

    res = egs_factory.client_for(admin).post(f"/api/bookings/{booking.pk}/cancel/", {}, format="json")
    assert res.status_code == 200, res.data
    assert _state(slot) == (SlotStatus.RESERVED_EXTERNAL, FBR, None)
