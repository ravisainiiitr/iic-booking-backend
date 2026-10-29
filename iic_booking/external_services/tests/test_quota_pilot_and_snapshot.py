"""External slot share: test accounts are exempt, and a weekly snapshot taken before the week's slots existed is
taken again instead of blocking external users with a cap of 0 for good."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService
from iic_booking.equipment.models import ExternalWeeklySlotQuotaSnapshot
from iic_booking.users.models.user_type import UserType

from .conftest import make_user

pytestmark = pytest.mark.django_db


def _next_monday() -> date:
    today = timezone.localdate()
    return today + timedelta(days=7 - today.weekday())


def _validate(user, eq, day, n=1):
    return ExternalSlotQuotaService.validate_external_booking(user, eq, slot_dates=[day], slots_requested=n)


def test_test_account_is_not_limited_by_the_share(factory):
    eq = factory.equipment(external_slot_quota_percent=0)
    day = _next_monday()
    assert not _validate(make_user(UserType.EXTERNAL), eq, day).allowed
    assert _validate(make_user(UserType.EXTERNAL, is_test_account=True), eq, day).allowed


def test_test_account_bookings_do_not_use_the_share(factory):
    eq = factory.equipment(external_slot_quota_percent=50)
    monday = _next_monday()
    factory.slot(eq, factory.at(monday, 9))
    factory.booking(make_user(UserType.EXTERNAL, is_test_account=True), eq, factory.at(monday, 10))
    week_start, week_end = ExternalSlotQuotaService.week_bounds_for_date(monday)
    assert ExternalSlotQuotaService.count_external_usage(eq, week_start, week_end) == 0
    factory.booking(make_user(UserType.EXTERNAL), eq, factory.at(monday, 11))
    assert ExternalSlotQuotaService.count_external_usage(eq, week_start, week_end) == 1


def test_empty_snapshot_is_taken_again_once_the_week_has_slots(factory):
    eq = factory.equipment(external_slot_quota_percent=0)
    monday = _next_monday()
    week_start, week_end = ExternalSlotQuotaService.week_bounds_for_date(monday)
    # Taken two weeks ahead, before the slots existed and before the share was set: a cap of 0.
    ExternalWeeklySlotQuotaSnapshot.objects.create(
        equipment=eq, week_start=week_start, week_end=week_end, total_bookable_slots=0, external_quota_percent=0,
        max_external_slots=0,
    )
    for h in range(9, 19):
        factory.slot(eq, factory.at(monday, h))
    eq.external_slot_quota_percent = 20
    eq.save(update_fields=["external_slot_quota_percent"])
    result = _validate(make_user(UserType.EXTERNAL), eq, monday)
    assert result.allowed, result.message
    snap = ExternalWeeklySlotQuotaSnapshot.objects.get(equipment=eq, week_start=week_start)
    assert (snap.total_bookable_slots, snap.external_quota_percent, snap.max_external_slots) == (10, 20, 2)


def _snapshot_with_slots(factory, percent):
    eq = factory.equipment(external_slot_quota_percent=percent)
    monday = _next_monday()
    week_start, _week_end = ExternalSlotQuotaService.week_bounds_for_date(monday)
    for h in range(9, 13):
        factory.slot(eq, factory.at(monday, h))
    first = ExternalSlotQuotaService.ensure_snapshot(eq, week_start)  # generates the rest of the week's slots too
    assert first.total_bookable_slots > 4
    return eq, monday, first


def _set_percent(eq, percent):
    eq.external_slot_quota_percent = percent
    eq.save(update_fields=["external_slot_quota_percent"])


def test_a_raised_share_applies_to_a_snapshotted_week_at_once(factory):
    eq, monday, first = _snapshot_with_slots(factory, 0)
    _set_percent(eq, 20)
    assert _validate(make_user(UserType.EXTERNAL), eq, monday).allowed
    snap = ExternalWeeklySlotQuotaSnapshot.objects.get(pk=first.pk)
    assert (snap.total_bookable_slots, snap.external_quota_percent) == (first.total_bookable_slots, 20)
    assert snap.max_external_slots == first.total_bookable_slots * 20 // 100


def test_a_lowered_share_waits_for_the_next_snapshot(factory):
    eq, monday, first = _snapshot_with_slots(factory, 50)
    _set_percent(eq, 0)
    assert _validate(make_user(UserType.EXTERNAL), eq, monday).allowed
    snap = ExternalWeeklySlotQuotaSnapshot.objects.get(pk=first.pk)
    assert (snap.total_bookable_slots, snap.external_quota_percent) == (first.total_bookable_slots, 50)
