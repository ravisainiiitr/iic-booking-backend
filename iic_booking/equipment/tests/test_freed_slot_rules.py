"""A slot a booking gives back, by the time left until it starts: more than 48 hours, allocated from the waitlist in
queue order; 48 hours down to just over 1 hour, active waitlist users are told; 1 hour or less, closed to users until
it starts, when it becomes an ordinary past slot (No booking). Staff can allocate it throughout."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import api_views, waitlist
from iic_booking.equipment.freed_slot_rules import (
    USERS_SEE_LABEL,
    exclude_freed_slots_locked_for_users,
    freed_slot_locked_for_users,
)
from iic_booking.equipment.maintenance_policy import ANALYSIS_NOT_POSSIBLE_SLOT_LABEL, disruption_freed_slot_fields
from iic_booking.equipment.models import (
    BookingDisruptionKind,
    DailySlot,
    EquipmentManager,
    SlotStatus,
    WaitlistEntry,
)
from iic_booking.equipment.slot_utils import SlotAvailabilityChecker
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def spy(monkeypatch):
    calls = SimpleNamespace(booked=[], offered=[])

    def _book(equipment, user, slot_ids, **kwargs):
        calls.booked.append(list(slot_ids))
        return None, "spy"

    def _offer(equipment, *, entries, slot_start_dt, lead_hours):
        calls.offered.append({"users": [e.user_id for e in entries], "start": slot_start_dt, "lead": lead_hours})
        return len(entries)

    monkeypatch.setattr(waitlist, "create_booking_for_waitlist_user", _book)
    monkeypatch.setattr(waitlist, "reduce_waitlist_inputs_to_fit_available_slots", lambda *a, **k: ({}, 60, 1))
    monkeypatch.setattr(waitlist, "_notify_waitlist_short_notice_slot_available", _offer)
    return calls


def _freed_slot(f, eq, start, **update):
    """A slot whose booking has just given it back."""
    slot = f.slot(eq, start)
    booking = f.booking(f.student(), eq, start + timedelta(days=30))
    DailySlot.objects.filter(pk=slot.pk).update(booking=booking, status=SlotStatus.BOOKED)
    DailySlot.objects.filter(pk=slot.pk).update(booking=None, status=update.get("status", SlotStatus.AVAILABLE))
    return DailySlot.objects.get(pk=slot.pk)


@pytest.fixture
def world(egs_factory):
    eq = egs_factory.equipment(waitlist_queue_depth=5)
    return SimpleNamespace(f=egs_factory, eq=eq, now=timezone.now().replace(microsecond=0))


def _at(now):
    return patch("django.utils.timezone.now", return_value=now)


@pytest.mark.parametrize(
    "lead,allocated,offered",
    [
        (timedelta(hours=48, minutes=1), True, False),
        (timedelta(hours=72), True, False),
        (timedelta(hours=48), False, True),
        (timedelta(hours=47), False, True),
        (timedelta(hours=1, minutes=1), False, True),
        (timedelta(hours=1), False, False),
        (timedelta(minutes=30), False, False),
        (timedelta(minutes=-1), False, False),
    ],
)
def test_time_left_decides_allocate_inform_or_neither(world, spy, lead, allocated, offered):
    WaitlistEntry.objects.create(equipment=world.eq, user=world.f.student(), status="ACTIVE")
    slot = _freed_slot(world.f, world.eq, world.now + lead)
    with _at(world.now):
        waitlist.notify_waitlist_slots_available(world.eq, preferred_slot_ids=[slot.pk])
    assert bool(spy.booked) is allocated
    assert bool(spy.offered) is offered


def test_a_booking_giving_back_slots_on_both_sides_of_48_hours_is_split(world, spy):
    WaitlistEntry.objects.create(equipment=world.eq, user=world.f.student(), status="ACTIVE")
    far = _freed_slot(world.f, world.eq, world.now + timedelta(hours=60))
    near = _freed_slot(world.f, world.eq, world.now + timedelta(hours=5))
    nearer = _freed_slot(world.f, world.eq, world.now + timedelta(hours=3))
    locked = _freed_slot(world.f, world.eq, world.now + timedelta(minutes=20))
    with _at(world.now):
        waitlist.notify_waitlist_slots_available(world.eq, preferred_slot_ids=[locked.pk, nearer.pk, near.pk, far.pk])
    assert spy.booked == [[far.pk]]
    assert [o["start"] for o in spy.offered] == [nearer.start_datetime]


def test_only_active_waitlist_candidates_are_informed(world, spy):
    active = world.f.student()
    WaitlistEntry.objects.create(equipment=world.eq, user=active, status="ACTIVE")
    WaitlistEntry.objects.create(equipment=world.eq, user=world.f.student(), status="OPT_OUT")
    WaitlistEntry.objects.create(equipment=world.eq, user=world.f.student(), status="CANNOT_FULFILL")
    slot = _freed_slot(world.f, world.eq, world.now + timedelta(hours=10))
    with _at(world.now):
        waitlist.notify_waitlist_slots_available(world.eq, preferred_slot_ids=[slot.pk])
    assert spy.offered[0]["users"] == [active.pk]
    assert WaitlistEntry.objects.filter(equipment=world.eq).count() == 3


def test_a_slot_freed_as_operator_absent_is_not_offered(world, spy):
    WaitlistEntry.objects.create(equipment=world.eq, user=world.f.student(), status="ACTIVE")
    slot = _freed_slot(world.f, world.eq, world.now + timedelta(hours=10), status=SlotStatus.OPERATOR_ABSENT)
    with _at(world.now):
        waitlist.notify_waitlist_slots_available(world.eq, preferred_slot_ids=[slot.pk])
    assert spy.booked == [] and spy.offered == []


def test_rules_do_not_depend_on_the_equipment_reschedule_setting(egs_factory, spy):
    eq = egs_factory.equipment(waitlist_queue_depth=5, reschedule_hours_threshold=6)
    now = timezone.now().replace(microsecond=0)
    WaitlistEntry.objects.create(equipment=eq, user=egs_factory.student(), status="ACTIVE")
    slot = _freed_slot(egs_factory, eq, now + timedelta(hours=10))
    with _at(now):
        waitlist.notify_waitlist_slots_available(eq, preferred_slot_ids=[slot.pk])
    assert spy.booked == [] and len(spy.offered) == 1


def test_sweep_without_given_slots_allocates_only_beyond_48_hours_and_tells_nobody(world, spy):
    WaitlistEntry.objects.create(equipment=world.eq, user=world.f.student(), status="ACTIVE")
    far = _freed_slot(world.f, world.eq, world.now + timedelta(hours=60))
    _freed_slot(world.f, world.eq, world.now + timedelta(hours=10))
    with _at(world.now):
        waitlist.notify_waitlist_slots_available(world.eq)
    assert spy.booked == [[far.pk]]
    assert spy.offered == []


# --- closed to users in the last hour ---------------------------------------------------------------------------


def _calendar_row(world, slot, user):
    client = APIClient() if user is None else world.f.client_for(user)
    day = timezone.localtime(slot.start_datetime).date().isoformat()
    res = client.get(f"/api/equipments/{world.eq.pk}/slots/", {"start_date": day, "end_date": day})
    assert res.status_code == 200, res.data
    body = res.data if isinstance(res.data, list) else res.data.get("slots") or []
    return next(r for r in body if r["id"] == slot.pk)


def _oic(world):
    oic = UserFactory(user_type=UserType.MANAGER, department=world.f.department)
    EquipmentManager.objects.create(equipment=world.eq, manager=oic)
    return oic


@pytest.mark.parametrize("viewer", ["anonymous", "student"])
def test_users_see_not_available_in_the_last_hour_then_no_booking(world, viewer):
    user = None if viewer == "anonymous" else world.f.student()
    start = world.now + timedelta(hours=3)
    slot = _freed_slot(world.f, world.eq, start)

    with _at(start - timedelta(hours=1, minutes=1)):
        assert _calendar_row(world, slot, user)["status"] == SlotStatus.AVAILABLE
    with _at(start - timedelta(hours=1)):
        row = _calendar_row(world, slot, user)
        assert (row["status"], row["status_display"]) == ("NOT_AVAILABLE", USERS_SEE_LABEL)
        assert row["available_for_external"] is False
    with _at(start - timedelta(seconds=1)):
        assert _calendar_row(world, slot, user)["status"] == "NOT_AVAILABLE"
    # At the start the slot is an ordinary past Available slot again, which calendars label No booking.
    with _at(start):
        assert _calendar_row(world, slot, user)["status"] == SlotStatus.AVAILABLE
    slot.refresh_from_db()
    assert slot.status == SlotStatus.AVAILABLE


def test_staff_see_the_real_status_and_what_users_see(world):
    slot = _freed_slot(world.f, world.eq, world.now + timedelta(minutes=30))
    with _at(world.now):
        row = _calendar_row(world, slot, _oic(world))
    assert row["status"] == SlotStatus.AVAILABLE
    assert row["users_blocked_label"] == USERS_SEE_LABEL
    assert "allocate" in row["users_blocked_reason"]


def test_never_booked_and_staff_opened_slots_stay_open_in_the_last_hour(world):
    start = world.now + timedelta(minutes=30)
    never_booked = world.f.slot(world.eq, start)
    reopened = _freed_slot(world.f, world.eq, start + timedelta(minutes=1))
    DailySlot.objects.filter(pk=reopened.pk).update(status=SlotStatus.BLOCKED)
    DailySlot.objects.filter(pk=reopened.pk).update(status=SlotStatus.AVAILABLE)
    reopened.refresh_from_db()
    with _at(world.now):
        assert not freed_slot_locked_for_users(never_booked)
        assert not freed_slot_locked_for_users(reopened)
        assert SlotAvailabilityChecker.is_slot_available(never_booked)
        assert _calendar_row(world, never_booked, world.f.student())["status"] == SlotStatus.AVAILABLE


def test_checker_and_query_agree_and_staff_are_not_locked_out(world):
    locked = _freed_slot(world.f, world.eq, world.now + timedelta(minutes=30))
    open_slot = _freed_slot(world.f, world.eq, world.now + timedelta(hours=2))
    with _at(world.now):
        assert not SlotAvailabilityChecker.is_slot_available(locked)
        assert not SlotAvailabilityChecker.is_slot_available_for_external(locked)
        assert SlotAvailabilityChecker.is_slot_available_for_staff(locked)
        assert SlotAvailabilityChecker.is_slot_available(open_slot)
    qs = DailySlot.objects.filter(pk__in=[locked.pk, open_slot.pk])
    assert list(exclude_freed_slots_locked_for_users(qs, world.now).values_list("pk", flat=True)) == [open_slot.pk]


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _body(slot):
    return {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
    }


def test_a_user_cannot_book_a_locked_slot_but_the_oic_can_book_it_for_them(world, monkeypatch, no_portal_lock):
    from decimal import Decimal

    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
    from iic_booking.users.repositories.wallet_repository import SubWalletRepository

    student = world.f.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=world.f.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    SubWalletRepository.get_or_create(wallet, world.f.department).credit(Decimal("1000.00"), description="Recharge")
    slot = _freed_slot(world.f, world.eq, timezone.now() + timedelta(minutes=30))
    monkeypatch.setattr(api_views, "_actor_may_book_on_behalf", lambda actor, equipment: None)

    res = world.f.client_for(student).post(f"/api/equipments/{world.eq.pk}/book/", _body(slot), format="json")
    assert res.status_code >= 400
    slot.refresh_from_db()
    assert slot.booking_id is None

    res = world.f.client_for(_oic(world)).post(
        f"/api/equipments/{world.eq.pk}/book/", {**_body(slot), "user_id": student.pk}, format="json"
    )
    assert res.status_code in (200, 201), res.data
    slot.refresh_from_db()
    assert slot.booking is not None and slot.booking.user_id == student.pk


# --- Analysis Not Possible -------------------------------------------------------------------------------------


def test_analysis_not_possible_frees_slots_not_started_and_keeps_the_label_on_started_ones(world):
    student = world.f.student()
    booking = world.f.booking(
        student, world.eq, world.now + timedelta(days=4), disruption_kind=BookingDisruptionKind.OTHER_DISRUPTION
    )
    ahead = booking.daily_slots.get()
    started = world.f.slot(world.eq, world.now - timedelta(hours=1))
    DailySlot.objects.filter(pk=started.pk).update(booking=booking, status=SlotStatus.BOOKED)

    booking.daily_slots.update(booking=None, **disruption_freed_slot_fields(booking, disrupted=True))

    ahead.refresh_from_db()
    started.refresh_from_db()
    assert (ahead.status, ahead.blocked_label, ahead.booking_id) == (SlotStatus.AVAILABLE, None, None)
    assert ahead.released_by_booking_at is not None
    assert (started.status, started.blocked_label) == (SlotStatus.BLOCKED, ANALYSIS_NOT_POSSIBLE_SLOT_LABEL)
