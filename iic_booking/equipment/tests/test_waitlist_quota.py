"""Waitlist and weekly / monthly quota.

Reported case (anonymised): a student whose weekly XPS-style group quota (200 min individual) is
already mostly used must not be waitlisted, and must not be auto-confirmed from the waitlist, when the
new request would go over. Automatic paths skip such entries (they stay waitlisted) and give the freed
slot to the next entry; OIC manual confirmation is allowed but warns.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from iic_booking.equipment import api_views, waitlist
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    DailySlot,
    EquipmentGroupQuota,
    EquipmentManager,
    QuotaType,
    WaitlistEntry,
)
from iic_booking.equipment.waitlist_quota import note_waitlist_join_request, waitlist_join_request_scope
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

WEEKLY_LIMIT = 200
USED_MINUTES = 144


def _monday(weeks_ahead: int):
    today = timezone.localdate()
    return today + timedelta(days=7 * weeks_ahead - today.weekday())


def _at(day, hour):
    return timezone.make_aware(datetime.combine(day, time(hour)), timezone.get_current_timezone())


@pytest.fixture
def world(egs_factory, settings):
    settings.SKIP_BOOKING_QUOTA_CHECK = False
    group = egs_factory.group()
    eq = egs_factory.equipment(group, unit_charge="0", waitlist_queue_depth=5, reschedule_hours_threshold=0)
    for quota_type, individual in ((QuotaType.WEEKLY, WEEKLY_LIMIT), (QuotaType.MONTHLY, 4000)):
        EquipmentGroupQuota.objects.create(
            equipment_group=group,
            quota_type=quota_type,
            internal_individual_quota_minutes=individual,
            internal_faculty_quota_minutes=9000,
            external_individual_quota_minutes=individual,
            external_faculty_quota_minutes=9000,
            is_enforced=True,
        )
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return SimpleNamespace(f=egs_factory, eq=eq, oic=oic, week=_monday(2))


@pytest.fixture
def wallet_ok():
    target = MagicMock()
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked", return_value=(False, "")
    ), patch(
        "iic_booking.equipment.waitlist_booking.WalletRepository.get_booking_wallet_target",
        return_value=(target, None),
    ), patch(
        "iic_booking.users.wallet_credit_facility.subwallet_booking_balance_ok", return_value=(True, "")
    ), patch(
        "iic_booking.users.student_spending_limits.spending_limit_error", return_value=None
    ):
        yield


@pytest.fixture
def one_slot_requests(monkeypatch):
    monkeypatch.setattr(waitlist, "reduce_waitlist_inputs_to_fit_available_slots", lambda *a, **k: ({}, 60, 1))


def _near_full_student(w, day):
    """Student who already used 144 of 200 weekly minutes in the week of ``day``."""
    student = w.f.student()
    booking = w.f.booking(student, w.eq, _at(day, 9), slot_count=3)
    Booking.objects.filter(pk=booking.pk).update(total_time_minutes=USED_MINUTES)
    return student


def _released_slot(w, day, hour=15):
    slot = w.f.slot(w.eq, _at(day, hour))
    DailySlot.objects.filter(pk=slot.pk).update(released_by_booking_at=timezone.now())
    return slot


def _queue(w, *users):
    entries = []
    for i, user in enumerate(users):
        entry = WaitlistEntry.objects.create(equipment=w.eq, user=user, status="ACTIVE")
        WaitlistEntry.objects.filter(pk=entry.pk).update(created_at=timezone.now() - timedelta(hours=10 - i))
        entries.append(entry)
    return entries


# --- automatic confirmation (cancellation / reschedule release, pre-reference sweep) -------------------


def test_auto_confirm_within_quota_allocates(world, wallet_ok, one_slot_requests):
    w = world
    student = w.f.student()
    (entry,) = _queue(w, student)
    slot = _released_slot(w, w.week + timedelta(days=1))

    assert waitlist.notify_waitlist_slots_available(w.eq, preferred_slot_ids=[slot.pk]) == 1

    slot.refresh_from_db()
    assert slot.booking.user_id == student.pk
    assert not WaitlistEntry.objects.filter(pk=entry.pk).exists()


def test_over_quota_entry_is_skipped_and_next_entry_gets_the_slot(world, wallet_ok, one_slot_requests):
    w = world
    over = _near_full_student(w, w.week)
    nxt = w.f.student()
    first, second = _queue(w, over, nxt)
    slot = _released_slot(w, w.week + timedelta(days=2))

    assert waitlist.notify_waitlist_slots_available(w.eq, preferred_slot_ids=[slot.pk]) == 1

    slot.refresh_from_db()
    assert slot.booking.user_id == nxt.pk
    first.refresh_from_db()
    assert first.status == "ACTIVE"
    assert "quota" in (first.cannot_fulfill_remark or "").lower()
    assert first.marked_cannot_fulfill_at is None
    assert not WaitlistEntry.objects.filter(pk=second.pk).exists()
    assert not Booking.objects.filter(user=over, daily_slots=slot).exists()


def test_over_quota_entry_alone_stays_waitlisted_and_slot_stays_free(world, wallet_ok, one_slot_requests):
    w = world
    over = _near_full_student(w, w.week)
    (entry,) = _queue(w, over)
    slot = _released_slot(w, w.week + timedelta(days=3))

    assert waitlist.notify_waitlist_slots_available(w.eq, preferred_slot_ids=[slot.pk]) == 0

    slot.refresh_from_db()
    assert slot.booking_id is None
    entry.refresh_from_db()
    assert entry.status == "ACTIVE"


def test_quota_is_counted_in_the_freed_slots_week(world, wallet_ok, one_slot_requests):
    w = world
    over = _near_full_student(w, w.week)
    (entry,) = _queue(w, over)
    next_week_slot = _released_slot(w, w.week + timedelta(days=8))

    assert waitlist.notify_waitlist_slots_available(w.eq, preferred_slot_ids=[next_week_slot.pk]) == 1

    next_week_slot.refresh_from_db()
    assert next_week_slot.booking.user_id == over.pk
    assert not WaitlistEntry.objects.filter(pk=entry.pk).exists()


def test_pre_reference_sweep_skips_over_quota_entry(world, wallet_ok, one_slot_requests):
    w = world
    over = _near_full_student(w, w.week)
    nxt = w.f.student()
    _queue(w, over, nxt)
    slot = _released_slot(w, w.week + timedelta(days=1))

    assert waitlist.notify_waitlist_slots_available(w.eq) == 1

    slot.refresh_from_db()
    assert slot.booking.user_id == nxt.pk


# --- joining the waitlist after a failed booking ------------------------------------------------------


def _fail_into_waitlist(w, user, *, skip_limits=False, week=None):
    with waitlist_join_request_scope():
        note_waitlist_join_request(
            input_values={}, slot_ids=[], week_start=(week or w.week).isoformat(), skip_limits=skip_limits
        )
        return api_views._enrich_failed_booking_response(
            w.eq, user, "Booking unsuccessful. All slots are occupied.",
            waitlist_on_failure=True, slot_unavailable_failure=True,
        )


def test_failed_booking_over_quota_is_not_waitlisted(world):
    w = world
    over = _near_full_student(w, w.week)

    with patch.object(api_views, "_schedule_unsuccessful_booking_waitlist_email"):
        payload = _fail_into_waitlist(w, over)

    assert payload.get("waitlist_quota_exceeded") is True
    assert "Not added to the waitlist" in payload["error"]
    assert "waitlist_position" not in payload
    assert not WaitlistEntry.objects.filter(equipment=w.eq, user=over).exists()


def test_failed_booking_within_quota_or_by_staff_is_waitlisted(world):
    w = world
    fresh = w.f.student()
    over = _near_full_student(w, w.week)

    with patch.object(api_views, "_schedule_unsuccessful_booking_waitlist_email"):
        assert _fail_into_waitlist(w, fresh).get("waitlist_position") == 1
        assert _fail_into_waitlist(w, over, skip_limits=True).get("waitlist_position") == 2
        other_week = _near_full_student(w, w.week)
        assert _fail_into_waitlist(w, other_week, week=w.week + timedelta(days=7)).get("waitlist_position") == 3


# --- Officer In Charge manual confirmation ------------------------------------------------------------


def _confirm(w, entry, slot, **extra):
    return w.f.client_for(w.oic).post(
        f"/api/admin/equipment/{w.eq.pk}/waitlist-confirm/",
        {"entry_id": entry.id, "slot_ids": [slot.id], **extra},
        format="json",
    )


def test_manual_confirm_over_quota_warns_but_is_allowed(world, wallet_ok):
    w = world
    over = _near_full_student(w, w.week)
    (entry,) = _queue(w, over)
    slot = w.f.slot(w.eq, _at(w.week + timedelta(days=5), 10), status="NOT_AVAILABLE")

    preview = _confirm(w, entry, slot, preview=True)
    assert preview.status_code == 200, preview.data
    assert preview.data["preview"] is True
    warning = preview.data["quota_warning"]
    assert "weekly quota" in warning and f"{USED_MINUTES}/{WEEKLY_LIMIT}" in warning
    assert "Staff bookings skip limits" in warning
    assert preview.data["quota_failure"]["period"] == QuotaType.WEEKLY
    assert WaitlistEntry.objects.filter(pk=entry.pk).exists()
    assert DailySlot.objects.get(pk=slot.pk).booking_id is None

    res = _confirm(w, entry, slot)
    assert res.status_code == 201, res.data
    assert res.data["quota_warning"] == warning
    booking = Booking.objects.get(booking_id=res.data["booking_id"])
    assert booking.user_id == over.pk and booking.status == BookingStatus.BOOKED
    assert not WaitlistEntry.objects.filter(pk=entry.pk).exists()


def test_manual_confirm_within_quota_has_no_warning(world, wallet_ok):
    w = world
    student = w.f.student()
    (entry,) = _queue(w, student)
    slot = w.f.slot(w.eq, _at(w.week + timedelta(days=5), 10), status="NOT_AVAILABLE")

    preview = _confirm(w, entry, slot, preview=True)
    assert preview.status_code == 200, preview.data
    assert preview.data["quota_warning"] is None

    res = _confirm(w, entry, slot)
    assert res.status_code == 201, res.data
    assert res.data["quota_warning"] is None
