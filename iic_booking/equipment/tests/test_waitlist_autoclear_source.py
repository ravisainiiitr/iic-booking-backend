"""Waitlist auto-confirmation only uses slots a booking cancellation / reschedule gave back.

Slots made Available by an OIC / admin (Change Slot Status, dashboard calendar, Django admin), by the equipment
returning to Operational, by removing a repeat block or by slot generation never auto-confirm the waitlist,
not even from the pre-reference sweep. A cancelled slot that staff later block and re-open stays staff-made.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from iic_booking.equipment import api_views, waitlist
from iic_booking.equipment.booking_cancellation import parse_cancellation_request, perform_booking_cancellation
from iic_booking.equipment.maintenance_policy import apply_when_equipment_becomes_operational
from iic_booking.equipment.models import (
    BookingStatus,
    DailySlot,
    EquipmentManager,
    SlotStatus,
    WaitlistEntry,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def spy(monkeypatch):
    calls = SimpleNamespace(booked=[], short_notice=[])

    def _book(equipment, user, slot_ids, **kwargs):
        calls.booked.append(sorted(slot_ids))
        return None, "spy"

    def _short_notice(equipment, **kwargs):
        calls.short_notice.append(equipment.pk)
        return 0

    monkeypatch.setattr(waitlist, "create_booking_for_waitlist_user", _book)
    monkeypatch.setattr(waitlist, "reduce_waitlist_inputs_to_fit_available_slots", lambda *a, **k: ({}, 60, 1))
    monkeypatch.setattr(waitlist, "_notify_waitlist_short_notice_slot_available", _short_notice)
    return calls


def _admin(egs_factory):
    return UserFactory(
        user_type=UserType.ADMIN, is_staff=True, admin_approved=True, department=egs_factory.department
    )


def _setup(egs_factory, slot_count=1):
    eq = egs_factory.equipment(reschedule_hours_threshold=0)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(days=5, hour=9), slot_count=slot_count)
    WaitlistEntry.objects.create(equipment=eq, user=egs_factory.student(), status="ACTIVE")
    slots = list(booking.daily_slots.order_by("start_datetime"))
    return SimpleNamespace(eq=eq, owner=owner, booking=booking, slots=slots)


def _cancel(egs_factory, booking, slot_ids=None):
    perform_booking_cancellation(
        booking,
        slot_ids=slot_ids or list(booking.daily_slots.values_list("id", flat=True)),
        should_refund=False,
        cancel_notes="",
        actor=_admin(egs_factory),
        allow_started_slots=True,
    )


def _stamp(slot):
    return DailySlot.objects.values_list("released_by_booking_at", flat=True).get(pk=slot.pk)


@contextmanager
def _as_oic():
    with patch("iic_booking.users.rbac.user_has_admin_panel_access", return_value=False), patch(
        "config.admin_panel_access_api.user_can_access_admin_module", return_value=False
    ):
        yield


def _staff(egs_factory, role, eq):
    if role == "admin":
        return _admin(egs_factory), _noop()
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True, department=egs_factory.department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic, _as_oic()


@contextmanager
def _noop():
    yield


def _bulk_status(egs_factory, user, eq, slot_ids, new_status):
    return egs_factory.client_for(user).post(
        f"/api/admin/equipment/{eq.pk}/bulk-slot-status/",
        {"slot_ids": slot_ids, "status": new_status, "source": "DASHBOARD_CALENDAR"},
        format="json",
    )


# --- triggers that auto-confirm ----------------------------------------------------------------


def test_cancellation_frees_slot_and_waitlist_is_auto_confirmed(egs_factory, spy, django_capture_on_commit_callbacks):
    w = _setup(egs_factory)
    with django_capture_on_commit_callbacks(execute=True):
        _cancel(egs_factory, w.booking)

    assert _stamp(w.slots[0]) is not None
    assert spy.booked == [[w.slots[0].pk]]


def test_partial_cancellation_auto_confirms_only_released_slot(
    egs_factory, spy, django_capture_on_commit_callbacks
):
    w = _setup(egs_factory, slot_count=2)
    parsed = parse_cancellation_request({"slot_ids": [w.slots[1].pk]}, w.booking)
    with django_capture_on_commit_callbacks(execute=True):
        perform_booking_cancellation(
            w.booking,
            slot_ids=parsed["slot_ids"],
            should_refund=False,
            cancel_notes="",
            actor=_admin(egs_factory),
            allow_started_slots=True,
            partial_plan=parsed["plan"],
        )

    assert _stamp(w.slots[0]) is None
    assert spy.booked == [[w.slots[1].pk]]


@pytest.mark.parametrize("endpoint", ["reschedule", "user-reschedule"])
def test_reschedule_frees_old_slot_and_waitlist_is_auto_confirmed(
    egs_factory, egs_quiet_side_effects, spy, monkeypatch, endpoint
):
    monkeypatch.setattr(api_views, "notify_waitlist_slots_available", waitlist.notify_waitlist_slots_available)
    w = _setup(egs_factory)
    new_slot = egs_factory.slot(w.eq, egs_factory.future(days=6, hour=11))
    actor = _admin(egs_factory) if endpoint == "reschedule" else w.owner

    res = egs_factory.client_for(actor).post(
        f"/api/bookings/{w.booking.pk}/{endpoint}/",
        {"start_time": new_slot.start_datetime.isoformat(), "end_time": new_slot.end_datetime.isoformat()},
        format="json",
    )

    assert res.status_code == 200, res.data
    assert _stamp(w.slots[0]) is not None
    assert _stamp(new_slot) is None
    assert spy.booked == [[w.slots[0].pk]]


def test_sweep_still_uses_slots_freed_by_cancellation(egs_factory, spy):
    w = _setup(egs_factory)
    _cancel(egs_factory, w.booking)  # on_commit not executed: the slot waits for the sweep
    plain = egs_factory.slot(w.eq, egs_factory.future(days=4, hour=9))

    waitlist.notify_waitlist_slots_available(w.eq)

    assert spy.booked == [[w.slots[0].pk]]
    assert plain.pk not in spy.booked[0]


# --- staff actions never auto-confirm ----------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "oic"])
def test_staff_marking_slots_available_does_not_auto_confirm(egs_factory, spy, role, django_capture_on_commit_callbacks):
    eq = egs_factory.equipment(reschedule_hours_threshold=0)
    WaitlistEntry.objects.create(equipment=eq, user=egs_factory.student(), status="ACTIVE")
    slot = egs_factory.slot(eq, egs_factory.future(days=5, hour=9), status=SlotStatus.BLOCKED)
    user, ctx = _staff(egs_factory, role, eq)

    with ctx, django_capture_on_commit_callbacks(execute=True):
        res = _bulk_status(egs_factory, user, eq, [slot.pk], SlotStatus.AVAILABLE)

    assert res.status_code == 200, res.data
    slot.refresh_from_db()
    assert slot.status == SlotStatus.AVAILABLE and slot.released_by_booking_at is None
    assert spy.booked == [] and spy.short_notice == []
    waitlist.notify_waitlist_slots_available(eq)
    assert spy.booked == []
    assert WaitlistEntry.objects.filter(equipment=eq, status="ACTIVE").count() == 1


def test_cancelled_slot_blocked_then_reopened_by_oic_does_not_auto_confirm(egs_factory, spy):
    w = _setup(egs_factory)
    _cancel(egs_factory, w.booking)
    assert _stamp(w.slots[0]) is not None
    user, ctx = _staff(egs_factory, "oic", w.eq)

    with ctx:
        assert _bulk_status(egs_factory, user, w.eq, [w.slots[0].pk], SlotStatus.BLOCKED).status_code == 200
        assert _bulk_status(egs_factory, user, w.eq, [w.slots[0].pk], SlotStatus.AVAILABLE).status_code == 200

    assert _stamp(w.slots[0]) is None
    waitlist.notify_waitlist_slots_available(w.eq)
    waitlist.notify_waitlist_slots_available(w.eq, preferred_slot_ids=[w.slots[0].pk])
    assert spy.booked == []


def test_blocking_a_booked_slot_refunds_but_never_hands_the_blocked_slot_to_the_waitlist(
    egs_factory, egs_quiet_side_effects, spy, django_capture_on_commit_callbacks
):
    w = _setup(egs_factory, slot_count=2)
    blocked, freed = w.slots
    admin = _admin(egs_factory)

    with patch(
        "iic_booking.users.repositories.wallet_repository.WalletRepository.get_booking_wallet_target",
        return_value=(MagicMock(), True),
    ), django_capture_on_commit_callbacks(execute=True):
        res = _bulk_status(egs_factory, admin, w.eq, [blocked.pk], SlotStatus.BLOCKED)

    assert res.status_code == 200, res.data
    w.booking.refresh_from_db()
    assert w.booking.status == BookingStatus.REFUNDED
    blocked.refresh_from_db()
    assert blocked.status == SlotStatus.BLOCKED and blocked.released_by_booking_at is None
    # The refunded booking's other slot was given back by the cancellation and is auto-confirmed.
    assert spy.booked == [[freed.pk]]


def test_equipment_back_to_operational_does_not_auto_confirm(egs_factory, spy):
    w = _setup(egs_factory)
    _cancel(egs_factory, w.booking)
    DailySlot.objects.filter(pk=w.slots[0].pk).update(status=SlotStatus.UNDER_MAINTENANCE)
    plain = egs_factory.slot(w.eq, egs_factory.future(days=4, hour=9), status=SlotStatus.UNDER_MAINTENANCE)

    apply_when_equipment_becomes_operational(w.eq)

    assert set(
        DailySlot.objects.filter(pk__in=[w.slots[0].pk, plain.pk]).values_list("status", "released_by_booking_at")
    ) == {(SlotStatus.AVAILABLE, None)}
    assert spy.booked == []
    waitlist.notify_waitlist_slots_available(w.eq)
    assert spy.booked == []


def test_django_admin_save_and_queryset_status_changes_clear_the_stamp(egs_factory):
    w = _setup(egs_factory, slot_count=2)
    _cancel(egs_factory, w.booking)
    first, second = (DailySlot.objects.get(pk=s.pk) for s in w.slots)
    assert first.released_by_booking_at and second.released_by_booking_at

    first.status = SlotStatus.BLOCKED
    first.save(update_fields=["status"])
    first.status = SlotStatus.AVAILABLE
    first.save()
    assert _stamp(first) is None

    second.blocked_label = "note only"
    second.save()
    assert _stamp(second) is not None
    DailySlot.objects.filter(pk=second.pk).update(home_department_only=True)
    assert _stamp(second) is not None
    DailySlot.objects.filter(pk=second.pk).update(status=SlotStatus.AVAILABLE)
    assert _stamp(second) is None


def test_release_update_stamps_only_slots_that_had_a_booking(egs_factory):
    w = _setup(egs_factory)
    loose = egs_factory.slot(w.eq, egs_factory.future(days=4, hour=9))

    DailySlot.objects.filter(pk__in=[w.slots[0].pk, loose.pk]).update(booking=None, status=SlotStatus.AVAILABLE)

    assert _stamp(w.slots[0]) is not None
    assert _stamp(loose) is None


def test_pre_reference_sweep_ignores_staff_made_available_slots(egs_factory, spy):
    eq = egs_factory.equipment(reschedule_hours_threshold=0, waitlist_queue_depth=5)
    WaitlistEntry.objects.create(equipment=eq, user=egs_factory.student(), status="ACTIVE")
    egs_factory.slot(eq, egs_factory.future(days=5, hour=9))
    reopened = egs_factory.slot(eq, egs_factory.future(days=5, hour=10), status=SlotStatus.BLOCKED)
    DailySlot.objects.filter(pk=reopened.pk).update(status=SlotStatus.AVAILABLE)

    base = timezone.localtime()
    now_dt = (base - timedelta(days=base.weekday() - 2)).replace(hour=10, minute=0, second=0, microsecond=0)
    eq.slot_window_reference_weekday = 2
    eq.slot_window_reference_time = time(10, 30)
    eq.save(update_fields=["slot_window_reference_weekday", "slot_window_reference_time"])

    deleted = waitlist.clear_waitlist_due_before_reference(now_dt=now_dt)

    assert spy.booked == []
    assert deleted == 1


def test_pre_reference_sweep_confirms_into_cancelled_slot(egs_factory, spy):
    w = _setup(egs_factory)
    w.eq.waitlist_queue_depth = 5
    w.eq.slot_window_reference_weekday = 2
    w.eq.slot_window_reference_time = time(10, 30)
    w.eq.save(update_fields=["waitlist_queue_depth", "slot_window_reference_weekday", "slot_window_reference_time"])
    _cancel(egs_factory, w.booking)
    egs_factory.slot(w.eq, egs_factory.future(days=4, hour=9))

    base = timezone.localtime()
    now_dt = (base - timedelta(days=base.weekday() - 2)).replace(hour=10, minute=0, second=0, microsecond=0)
    waitlist.clear_waitlist_due_before_reference(now_dt=now_dt)

    assert spy.booked == [[w.slots[0].pk]]
