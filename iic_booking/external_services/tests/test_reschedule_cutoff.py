"""External users reschedule a week's slots until Wednesday 20:30 of the week before (internal booking opens 21:00)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from iic_booking.external_services.guards import external_reschedule_cutoff, external_reschedule_refusal
from iic_booking.users.models.user_type import UserType

from .conftest import make_user

pytestmark = pytest.mark.django_db


def _local(y, m, d, hh, mm=0):
    return timezone.make_aware(datetime(y, m, d, hh, mm))


@pytest.mark.parametrize("slot", [
    _local(2026, 11, 9, 9),    # Monday of the week
    _local(2026, 11, 13, 17),  # Friday of the same week
    _local(2026, 11, 15, 10),  # Sunday of the same week
])
def test_cutoff_is_wednesday_2030_of_the_week_before(slot):
    assert external_reschedule_cutoff(slot) == _local(2026, 11, 4, 20, 30)


def test_external_user_is_refused_after_the_cutoff_and_allowed_before():
    user = make_user(UserType.EXTERNAL)
    slot = _local(2026, 11, 10, 9)
    assert external_reschedule_refusal(user, slot, now=_local(2026, 11, 4, 20, 30)) is None
    refusal = external_reschedule_refusal(user, slot, now=_local(2026, 11, 4, 20, 31))
    assert refusal["code"] == "EXTERNAL_RESCHEDULE_CUTOFF" and "booking details page" in refusal["error"]


def test_internal_users_are_not_bound_by_the_weekly_cutoff():
    user = make_user(UserType.FACULTY)
    assert external_reschedule_refusal(user, _local(2026, 11, 10, 9), now=_local(2026, 11, 9, 8)) is None


def test_the_current_week_is_past_its_cutoff():
    user = make_user(UserType.EXTERNAL)
    now = timezone.now()
    assert external_reschedule_refusal(user, now + timedelta(hours=1), now=now) is not None
