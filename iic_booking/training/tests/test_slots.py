"""Slot reservations: only AVAILABLE slots are taken; release restores; BOOKED/HOLD/disrupted never touched."""

import pytest
from django.db import connection

from iic_booking.equipment.models import DailySlot, SlotStatus
from iic_booking.training import slots as slot_service
from iic_booking.training.models import EventKind, SessionSlotReservation, SessionStatus, TrainingEvent, TrainingSession

from .conftest import at, future_day, make_slots


def _session(world, day, start_h, end_h):
    event = TrainingEvent.objects.create(kind=EventKind.HANDS_ON, title="FE-SEM basics", slug=f"ev-{start_h}-{day}", equipment=world.equipment)
    return TrainingSession.objects.create(event=event, equipment=world.equipment, start_at=at(day, start_h), end_at=at(day, end_h))


@pytest.mark.django_db
def test_reserve_blocks_available_slots_with_label_and_release_restores(world):
    day = future_day()
    slots = make_slots(world.equipment, day)
    session = _session(world, day, 10, 12)
    label = slot_service.demo_label(course="CY-501", faculty_name="Asha Rao")
    assert label == "Demo: CY-501 (Prof. Asha Rao)"

    result = slot_service.reserve_session_slots(session, actor=world.oic, label=label)
    assert len(result.reserved_slot_ids) == 2
    blocked = DailySlot.objects.filter(id__in=result.reserved_slot_ids)
    assert {s.status for s in blocked} == {SlotStatus.BLOCKED}
    assert {s.blocked_label for s in blocked} == {label}
    assert set(SessionSlotReservation.objects.filter(session=session).values_list("previous_status", flat=True)) == {SlotStatus.AVAILABLE}
    session.refresh_from_db()
    assert session.status == SessionStatus.SCHEDULED
    untouched = DailySlot.objects.exclude(id__in=result.reserved_slot_ids).filter(slot_master__equipment=world.equipment)
    assert {s.status for s in untouched} == {SlotStatus.AVAILABLE}

    out = slot_service.release_session_slots(session, actor=world.oic, note="test")
    assert sorted(out["restored_slot_ids"]) == sorted(result.reserved_slot_ids)
    assert set(DailySlot.objects.filter(id__in=result.reserved_slot_ids).values_list("status", flat=True)) == {SlotStatus.AVAILABLE}
    assert not SessionSlotReservation.objects.filter(session=session, released_at__isnull=True).exists()
    assert slots


@pytest.mark.django_db
@pytest.mark.parametrize("busy_status", [SlotStatus.BOOKED, SlotStatus.UNDER_MAINTENANCE, SlotStatus.OPERATOR_ABSENT, SlotStatus.BLOCKED])
def test_reserve_refuses_when_any_slot_is_not_free(world, busy_status):
    day = future_day()
    slots = make_slots(world.equipment, day)
    DailySlot.objects.filter(pk=slots[2].pk).update(status=busy_status)  # 11:00
    session = _session(world, day, 10, 12)
    with pytest.raises(slot_service.ReservationError) as exc:
        slot_service.reserve_session_slots(session, actor=world.oic, label="Training: x")
    assert exc.value.code == "slots_not_free"
    assert exc.value.conflicts[0]["slot_id"] == slots[2].pk
    assert DailySlot.objects.get(pk=slots[1].pk).status == SlotStatus.AVAILABLE
    assert DailySlot.objects.get(pk=slots[2].pk).status == busy_status
    assert not SessionSlotReservation.objects.exists()


@pytest.mark.django_db
def test_release_leaves_slot_that_changed_after_reservation(world):
    day = future_day()
    make_slots(world.equipment, day)
    session = _session(world, day, 9, 11)
    result = slot_service.reserve_session_slots(session, actor=world.oic, label="Training: x")
    changed = result.reserved_slot_ids[0]
    DailySlot.objects.filter(pk=changed).update(status=SlotStatus.UNDER_MAINTENANCE, blocked_label="")
    out = slot_service.release_session_slots(session, actor=world.oic)
    assert out["left_unchanged_slot_ids"] == [changed]
    assert DailySlot.objects.get(pk=changed).status == SlotStatus.UNDER_MAINTENANCE
    assert DailySlot.objects.get(pk=result.reserved_slot_ids[1]).status == SlotStatus.AVAILABLE


@pytest.mark.django_db
def test_reserve_requires_generated_contiguous_slots(world):
    day = future_day()
    make_slots(world.equipment, day, hours=[9, 11])
    session = _session(world, day, 9, 12)
    with pytest.raises(slot_service.ReservationError) as exc:
        slot_service.reserve_session_slots(session, actor=world.oic, label="x")
    assert exc.value.code in ("not_covered", "not_contiguous")


@pytest.mark.django_db
def test_lost_race_with_booking_rolls_back(world, monkeypatch):
    """If a booking grabs a slot between the locked read and the conditional update, nothing is reserved."""
    day = future_day()
    slots = make_slots(world.equipment, day)
    session = _session(world, day, 10, 12)
    original = slot_service._block
    calls = {"n": 0}

    def racing_block(slot, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            DailySlot.objects.filter(pk=slot.pk).update(status=SlotStatus.BOOKED)
        return original(slot, **kwargs)

    monkeypatch.setattr(slot_service, "_block", racing_block)
    with pytest.raises(slot_service.ReservationError) as exc:
        from django.db import transaction

        with transaction.atomic():
            slot_service.reserve_session_slots(session, actor=world.oic, label="x")
    assert exc.value.code == "slot_race"
    assert DailySlot.objects.get(pk=slots[1].pk).status == SlotStatus.AVAILABLE
    assert DailySlot.objects.get(pk=slots[1].pk).blocked_label in (None, "")
    assert not SessionSlotReservation.objects.exists()


@pytest.mark.django_db
def test_free_windows_skip_busy_slots(world):
    day = future_day()
    slots = make_slots(world.equipment, day)
    DailySlot.objects.filter(pk=slots[1].pk).update(status=SlotStatus.BOOKED)  # 10:00 booked
    windows = slot_service.free_windows(world.equipment, date_from=day, date_to=day, duration_minutes=120)
    from django.utils.dateparse import parse_datetime

    starts = [parse_datetime(w["start"]) for w in windows]
    assert at(day, 9) not in starts
    assert at(day, 10) not in starts
    assert at(day, 11) in starts


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="Row locks need PostgreSQL")
def test_concurrent_booking_and_reservation_postgres(world):
    import threading

    from django.db import transaction

    day = future_day()
    slots = make_slots(world.equipment, day)
    session = _session(world, day, 10, 11)
    target = slots[1]
    results = {}

    def book():
        with transaction.atomic():
            locked = DailySlot.objects.select_for_update().filter(pk=target.pk, status=SlotStatus.AVAILABLE).first()
            if locked:
                DailySlot.objects.filter(pk=target.pk).update(status=SlotStatus.BOOKED)
                results["booked"] = True

    def reserve():
        try:
            slot_service.reserve_session_slots(session, actor=None, label="x")
            results["reserved"] = True
        except slot_service.ReservationError:
            results["reserved"] = False

    threads = [threading.Thread(target=book), threading.Thread(target=reserve)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.get("booked") != results.get("reserved")
